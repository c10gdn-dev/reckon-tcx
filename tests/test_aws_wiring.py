"""The parts that assemble a Lambda: the queue seam, configuration, and the two
`handler` entry points Terraform will name in phase 7.

Kept separate from `test_receiver.py` and `test_worker.py`, which test the logic
those handlers delegate to with plain dictionaries. What is here is the wiring:
environment variables in, real (mocked) AWS clients out.
"""

import json
from dataclasses import replace
from typing import Any

import pytest

from conftest import REGION, TABLE
from fakes import Clock, FakeTransport, response
from reckon.aws import receiver, warden, worker
from reckon.aws.config import build_pipeline, from_environment
from reckon.aws.queue import Sqs
from reckon.aws.secrets import Secrets, parameter_name
from reckon.clients.health import Profile
from reckon.clients.oauth import Tokens
from reckon.core.errors import ReckonError
from reckon.stores.base import Status
from reckon.stores.dynamo import DynamoStore

LIVE = Tokens("live-access", "refresh", 4_000_000_000.0)
DAY = 86400.0


def received(client: Any, url: str) -> list[dict[str, Any]]:
    messages = client.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(m["Body"]) for m in messages]


# --- the queue seam ---------------------------------------------------------


def test_a_message_reaches_the_queue(sqs) -> None:
    client, url = sqs
    Sqs(url, client=client).send({"type": "notification", "body": "{}"})
    assert received(client, url) == [{"type": "notification", "body": "{}"}]


def test_a_delayed_message_is_accepted(sqs) -> None:
    """`DelaySeconds` is how the worker waits without sleeping."""
    client, url = sqs
    Sqs(url, client=client).send({"type": "upload_check"}, delay_seconds=20)
    assert received(client, url) == [], "not visible yet, which is the point"


def test_the_client_is_built_lazily(aws_credentials: None) -> None:
    queue = Sqs("https://sqs.example/none")
    assert queue._injected is None
    assert queue.client is queue.client


# --- configuration ----------------------------------------------------------


def test_from_environment_reads_a_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_TEST_VALUE", "present")
    assert from_environment("RECKON_TEST_VALUE") == "present"


def test_a_missing_variable_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECKON_TEST_VALUE", raising=False)
    with pytest.raises(KeyError, match="RECKON_TEST_VALUE is not set"):
        from_environment("RECKON_TEST_VALUE")


def _secrets(table: str, profile: str | None = None):
    """A secret resolver that answers by name, as the real one does.

    A stub returning one value for every key hid a real failure once: the
    profile lookup got a table name and the enum raised a bare ValueError.
    """

    def resolve(name: str) -> str:
        if name == "RECKON_TABLE":
            return table
        if name == "RECKON_GOOGLE_PROFILE":
            if profile is None:
                raise KeyError(name)
            return profile
        return "x"

    return resolve


def _authorised(dynamo) -> DynamoStore:
    """A store with both services authorised, since `build_pipeline` reads tokens."""
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", LIVE, expected_version=0)
    store.save("strava", LIVE, expected_version=0)
    return store


def test_the_pipeline_is_assembled_from_secrets(dynamo) -> None:
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", LIVE, expected_version=0)
    store.save("strava", LIVE, expected_version=0)

    pipeline = build_pipeline(
        store=store,
        transport=FakeTransport(),
        secret={
            "RECKON_GOOGLE_CLIENT_ID": "g",
            "RECKON_GOOGLE_CLIENT_SECRET": "gs",
            "RECKON_STRAVA_CLIENT_ID": "s",
            "RECKON_STRAVA_CLIENT_SECRET": "ss",
        }.__getitem__,
        now=Clock(now=1000.0).time,
    )
    assert pipeline.logs is store
    assert pipeline.dry_run is False


def test_an_unauthorised_service_fails_at_assembly(dynamo) -> None:
    """Better than a Lambda that starts and then fails on its first request."""
    from reckon.pipeline import NotAuthorised

    with pytest.raises(NotAuthorised, match="google"):
        build_pipeline(
            store=DynamoStore(TABLE, client=dynamo),
            transport=FakeTransport(),
            secret=_secrets("reckon"),
        )


def test_the_store_defaults_to_dynamo_named_by_configuration(dynamo, monkeypatch) -> None:
    """No store passed means DynamoDB, named by the RECKON_TABLE secret."""
    monkeypatch.setattr(
        "reckon.aws.config.DynamoStore", lambda name, **kw: DynamoStore(name, client=dynamo, **kw)
    )
    DynamoStore(TABLE, client=dynamo).save("google", LIVE, expected_version=0)
    DynamoStore(TABLE, client=dynamo).save("strava", LIVE, expected_version=0)
    pipeline = build_pipeline(transport=FakeTransport(), secret=_secrets(TABLE))
    assert isinstance(pipeline.logs, DynamoStore)
    assert pipeline.logs.table_name == TABLE


# --- the receiver entry point -----------------------------------------------


def test_the_receiver_handler_enqueues(aws, monkeypatch: pytest.MonkeyPatch) -> None:
    _, sqs_client, url = aws
    monkeypatch.setenv("RECKON_WEBHOOK_SECRET", "Bearer s3cret")
    monkeypatch.setenv("RECKON_QUEUE_URL", url)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)

    result = receiver.handler(
        {"headers": {"authorization": "Bearer s3cret"}, "body": '{"dataType": "exercise"}'}
    )
    assert result["statusCode"] == receiver.DELIVERED
    (message,) = received(sqs_client, url)
    assert message["type"] == "notification"
    assert message["body"] == '{"dataType": "exercise"}'


def test_the_receiver_handler_refuses_a_bad_secret(aws, monkeypatch: pytest.MonkeyPatch) -> None:
    _, sqs_client, url = aws
    monkeypatch.setenv("RECKON_WEBHOOK_SECRET", "Bearer s3cret")
    monkeypatch.setenv("RECKON_QUEUE_URL", url)
    result = receiver.handler({"headers": {"authorization": "wrong"}, "body": "{}"})
    assert result["statusCode"] == receiver.UNAUTHORISED
    assert received(sqs_client, url) == []


# --- the worker entry point -------------------------------------------------


def test_the_worker_handler_settles_an_upload(aws, monkeypatch: pytest.MonkeyPatch) -> None:
    dynamo_client, _, url = aws
    store = DynamoStore(TABLE, client=dynamo_client)
    store.save("google", LIVE, expected_version=0)
    store.save("strava", LIVE, expected_version=0)

    for name, value in (
        ("RECKON_TABLE", TABLE),
        ("RECKON_QUEUE_URL", url),
        ("RECKON_GOOGLE_CLIENT_ID", "g"),
        ("RECKON_GOOGLE_CLIENT_SECRET", "gs"),
        ("RECKON_STRAVA_CLIENT_ID", "s"),
        ("RECKON_STRAVA_CLIENT_SECRET", "ss"),
    ):
        monkeypatch.setenv(name, value)

    finished = json.dumps(
        {"id": 987, "external_id": "1", "error": None, "activity_id": 555, "status": "ready"}
    ).encode()
    monkeypatch.setattr(
        "reckon.aws.config.retrying", lambda *_a, **_k: FakeTransport(response(body=finished))
    )
    event = {
        "Records": [
            {
                "body": json.dumps(
                    {
                        "type": "upload_check",
                        "strava_upload_id": 987,
                        "exercise_id": "889672",
                        "attempt": 1,
                    }
                )
            }
        ]
    }
    assert worker.handler(event) == {"processed": 0}
    assert store.get("889672").status is Status.UPLOADED
    assert store.get("889672").strava_activity_id == 555


# --- secrets ----------------------------------------------------------------
#
# Two sources on purpose. Non-secret names arrive as environment variables;
# client secrets are SecureStrings read from SSM at run time, because resolving
# them in Terraform would write the plaintext into state and into the function's
# visible configuration.


def test_a_parameter_name_is_derived_from_the_variable() -> None:
    """Derived rather than configured twice, so the two cannot drift apart."""
    assert parameter_name("RECKON_GOOGLE_CLIENT_ID") == "/reckon/google_client_id"
    assert parameter_name("RECKON_WEBHOOK_SECRET", "/other") == "/other/webhook_secret"


def test_the_environment_wins_over_ssm() -> None:
    """Which is what lets a laptop and a Lambda be configured identically."""
    secrets = Secrets(client=None, environ={"RECKON_TABLE": "from-env"}.get)
    assert secrets("RECKON_TABLE") == "from-env"


def test_a_secret_absent_from_the_environment_comes_from_ssm(ssm) -> None:
    client, _ = ssm
    secrets = Secrets(client=client, environ=lambda _: None)
    assert secrets("RECKON_GOOGLE_CLIENT_SECRET") == "ssm-value"


def test_an_ssm_read_is_cached_for_the_life_of_the_container(ssm) -> None:
    client, calls = ssm
    secrets = Secrets(client=client, environ=lambda _: None)
    secrets("RECKON_GOOGLE_CLIENT_SECRET")
    secrets("RECKON_GOOGLE_CLIENT_SECRET")
    assert calls == ["/reckon/google_client_secret"], "one call per cold start, not per use"


def test_a_missing_parameter_names_both_places_it_looked(ssm) -> None:
    client, _ = ssm
    secrets = Secrets(client=client, environ=lambda _: None)
    with pytest.raises(KeyError, match="RECKON_NOWHERE is not in the environment"):
        secrets("RECKON_NOWHERE")


def test_the_ssm_client_is_built_lazily(aws_credentials: None) -> None:
    secrets = Secrets()
    assert secrets._injected is None
    assert secrets.client is secrets.client


# --- profiles ---------------------------------------------------------------


def test_a_deployment_that_says_nothing_does_not_ask_for_the_restricted_scope(dynamo) -> None:
    """The safe way round: `published` not fetching costs a trace it could not
    have had, where `testing` misconfigured costs a 403 on every activity."""
    store = _authorised(dynamo)
    pipeline = build_pipeline(store=store, transport=FakeTransport(), secret=_secrets(TABLE))

    assert pipeline.merge_heart_rate is False


def test_the_testing_profile_fetches_the_heart_rate_series(dynamo) -> None:
    pipeline = build_pipeline(
        store=_authorised(dynamo),
        transport=FakeTransport(),
        secret=_secrets(TABLE, profile="testing"),
    )

    assert pipeline.merge_heart_rate is True


def test_a_misconfigured_profile_fails_at_startup_naming_what_is_allowed(dynamo) -> None:
    """Rather than as a bare ValueError, or as a 403 per activity hours later."""
    with pytest.raises(ReckonError, match="expected one of testing, published"):
        build_pipeline(
            store=_authorised(dynamo),
            transport=FakeTransport(),
            secret=_secrets(TABLE, profile="full"),
        )


def test_the_deployed_pipeline_gets_an_inventory(dynamo) -> None:
    """Deployed mode reconciles, and a pipeline without one refuses rather than
    answering "nothing is on Strava"."""
    store = _authorised(dynamo)
    pipeline = build_pipeline(store=store, transport=FakeTransport(), secret=_secrets(TABLE))

    assert pipeline.inventory is store


# --- the warden -------------------------------------------------------------
#
# The `testing` profile's grant dies after seven days and nothing reports a
# refresh token's expiry, so the only way to warn before it stops is to remember
# when a human last authorised.


def test_the_warden_reports_how_old_each_grant_is(dynamo) -> None:
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", replace(LIVE, authorised_at=0.0), expected_version=0)
    store.save("google", replace(LIVE, authorised_at=86400.0 * 10), expected_version=1)

    report = warden.check(store=store, now=Clock(now=86400.0 * 13).time)

    assert report["services"]["google"]["days"] == 3.0


def test_the_warden_warns_once_the_grant_is_near_seven_days(dynamo) -> None:
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", replace(LIVE, authorised_at=DAY), expected_version=0)

    testing = {"profile": Profile.TESTING}
    assert warden.check(store=store, now=Clock(now=DAY * 7.5).time, **testing)["warn"] is True
    assert warden.check(store=store, now=Clock(now=DAY * 4).time, **testing)["warn"] is False


def test_the_warden_never_warns_about_strava(dynamo) -> None:
    """Its grant lasts until revoked; the age is reported and not acted on."""
    store = DynamoStore(TABLE, client=dynamo)
    store.save("strava", replace(LIVE, authorised_at=DAY), expected_version=0)

    report = warden.check(store=store, now=Clock(now=DAY * 900).time)

    assert report["services"]["strava"]["watched"] is False
    assert report["warn"] is False


def test_the_warden_separates_never_authorised_from_never_recorded(dynamo) -> None:
    """Both give an unknown age and they are not the same thing, and neither is
    grounds for a warning — one that fires on "unknown" fires forever."""
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", LIVE, expected_version=0)  # authorised_at defaults to 0.0

    report = warden.check(store=store, now=Clock(now=DAY * 30).time)

    assert report["services"]["google"]["state"] == "authorised before this was recorded"
    assert report["services"]["strava"]["state"] == "not authorised"
    assert report["warn"] is False


def test_the_warden_handler_prints_the_report_for_the_metric_filter(
    dynamo, monkeypatch, capsys
) -> None:
    """A scheduled invocation's return value goes nowhere; the alarm reads the log.

    The filter matches `{"warn": true}` at the top level, so the shape of this
    line is load-bearing rather than cosmetic.
    """
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", replace(LIVE, authorised_at=DAY), expected_version=0)
    monkeypatch.setattr(warden, "DynamoStore", lambda name, **kw: store)
    monkeypatch.setenv("RECKON_TABLE", TABLE)
    monkeypatch.setenv("RECKON_GOOGLE_PROFILE", "testing")

    # The handler uses the real clock, and a grant stamped at day one of the
    # epoch is long past seven days — so this is the warning case, printed.
    warden.handler({}, None)

    logged = json.loads(capsys.readouterr().out)
    assert logged["warn"] is True
    assert logged["services"]["google"]["state"] == "ok"


def test_the_warden_handler_returns_the_report_rather_than_raising(dynamo, monkeypatch) -> None:
    """An expiring grant is news, not a fault. Raising would put it on the
    dead-letter queue beside activities that genuinely failed to reach Strava."""
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", replace(LIVE, authorised_at=DAY), expected_version=0)
    monkeypatch.setattr(warden, "DynamoStore", lambda name, **kw: store)
    monkeypatch.setenv("RECKON_TABLE", TABLE)

    report = warden.handler({})

    assert report["services"]["google"]["state"] == "ok"


def test_the_warden_does_not_warn_on_the_published_profile(dynamo) -> None:
    """A published client's grant lasts about six months, not seven days.

    The warden warned unconditionally until 2026-09-13, using the testing
    numbers against whatever was deployed. With `published` the default, the
    first run after migrating an existing grant would have emailed
    "re-authorise now" with months left — and an alarm that cries wolf in week
    one is worse than no alarm, because the operator learns to close it.
    """
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", replace(LIVE, authorised_at=DAY), expected_version=0)

    report = warden.check(store=store, now=Clock(now=DAY * 90).time, profile=Profile.PUBLISHED)

    assert report["services"]["google"]["watched"] is False
    assert report["warn"] is False
    assert report["profile"] == "published"


def test_the_warden_reads_the_profile_from_configuration(dynamo) -> None:
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", replace(LIVE, authorised_at=DAY), expected_version=0)

    report = warden.check(
        store=store, now=Clock(now=DAY * 90).time, secret=_secrets(TABLE, profile="testing")
    )

    assert report["warn"] is True


def test_an_unreadable_profile_warns_about_nothing(dynamo) -> None:
    """The failure that matters here is a false alarm, not a missed one.

    A missed warning costs a re-authorisation the operator must do anyway; a
    false one costs the credibility of the channel that reports real problems.
    """
    store = DynamoStore(TABLE, client=dynamo)
    store.save("google", replace(LIVE, authorised_at=DAY), expected_version=0)

    report = warden.check(
        store=store, now=Clock(now=DAY * 90).time, secret=_secrets(TABLE, profile="nonsense")
    )

    assert report["warn"] is False
