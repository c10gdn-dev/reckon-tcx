"""The parts that assemble a Lambda: the queue seam, configuration, and the two
`handler` entry points Terraform will name in phase 7.

Kept separate from `test_receiver.py` and `test_worker.py`, which test the logic
those handlers delegate to with plain dictionaries. What is here is the wiring:
environment variables in, real (mocked) AWS clients out.
"""

import json
from typing import Any

import pytest

from conftest import REGION, TABLE
from fakes import Clock, FakeTransport, response
from reckon.aws import receiver, worker
from reckon.aws.config import build_pipeline, from_environment
from reckon.aws.queue import Sqs
from reckon.aws.secrets import Secrets, parameter_name
from reckon.clients.oauth import Tokens
from reckon.stores.base import Status
from reckon.stores.dynamo import DynamoStore

LIVE = Tokens("live-access", "refresh", 4_000_000_000.0)


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
            secret=lambda name: "x",
        )


def test_the_store_defaults_to_dynamo_named_by_configuration(dynamo, monkeypatch) -> None:
    """No store passed means DynamoDB, named by the RECKON_TABLE secret."""
    monkeypatch.setattr(
        "reckon.aws.config.DynamoStore", lambda name, **kw: DynamoStore(name, client=dynamo, **kw)
    )
    DynamoStore(TABLE, client=dynamo).save("google", LIVE, expected_version=0)
    DynamoStore(TABLE, client=dynamo).save("strava", LIVE, expected_version=0)
    pipeline = build_pipeline(transport=FakeTransport(), secret=lambda name: TABLE)
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
