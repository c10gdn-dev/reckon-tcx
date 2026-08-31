"""The DynamoDB adapter's own concerns, under moto.

Behaviour shared with `stores/file.py` is in `test_store_contract.py`, which runs
one set of tests against both. What is here is what only this adapter has: the
conditional write, the TTL, read consistency, lazy client construction, and what
happens when the table holds something unreadable.

`moto` mocks in process — no Docker, no LocalStack, no credentials, no cost.
"""

import time

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from conftest import REGION, TABLE
from fakes import Clock
from reckon.clients.oauth import Tokens
from reckon.stores.base import LogEntry, Status, StoreError, TokenConflict
from reckon.stores.dynamo import LOG_TTL_DAYS, DynamoStore

TOKENS = Tokens("access", "refresh", 5000.0)


def store(client, **kwargs) -> DynamoStore:
    return DynamoStore(TABLE, client=client, now=Clock(now=1000.0).time, **kwargs)


def raw(client, key: str) -> dict:
    return client.get_item(TableName=TABLE, Key={"pk": {"S": key}}).get("Item", {})


# --- the compare-and-swap ---------------------------------------------------


def test_a_simulated_race_has_exactly_one_winner(dynamo) -> None:
    """Two workers, one table, both refreshing from version 1.

    The mechanism `PLAN.md` §9 relies on under a residual concurrency of 2.
    """
    a, b = store(dynamo), store(dynamo)
    a.save("google", TOKENS, expected_version=0)

    both = [a.load("google").version, b.load("google").version]
    assert both == [1, 1], "both workers see the same version"

    a.save("google", Tokens("winner", "refresh", 9000.0), expected_version=1)
    with pytest.raises(TokenConflict) as caught:
        b.save("google", Tokens("loser", "refresh", 9000.0), expected_version=1)

    assert (caught.value.expected, caught.value.found) == (1, 2)
    assert b.load("google").tokens.access_token == "winner"


def test_the_loser_can_continue_from_the_winners_version(dynamo) -> None:
    """Re-read and carry on; never repeat the refresh."""
    a, b = store(dynamo), store(dynamo)
    a.save("google", TOKENS, expected_version=0)
    a.save("google", Tokens("winner", "refresh", 1.0), expected_version=1)
    with pytest.raises(TokenConflict):
        b.save("google", Tokens("loser", "refresh", 1.0), expected_version=1)

    current = b.load("google")
    assert b.save("google", Tokens("later", "refresh", 1.0), expected_version=current.version)
    assert b.load("google").version == 3


def test_a_first_write_uses_attribute_not_exists_not_version_zero(dynamo) -> None:
    """`version = 0` would match nothing, so a first write needs its own condition."""
    subject = store(dynamo)
    subject.save("google", TOKENS, expected_version=0)
    with pytest.raises(TokenConflict, match="expected version 0, found 1"):
        subject.save("google", TOKENS, expected_version=0)


def test_an_unrelated_client_error_is_not_swallowed(dynamo) -> None:
    """Only a failed condition means a race; everything else is a real fault."""
    subject = DynamoStore("no-such-table", client=dynamo)
    with pytest.raises(ClientError, match=r"ResourceNotFoundException"):
        subject.save("google", TOKENS, expected_version=0)


# --- what lands in the table ------------------------------------------------


def test_tokens_are_keyed_by_service(dynamo) -> None:
    store(dynamo).save("google", TOKENS, expected_version=0)
    assert raw(dynamo, "TOKEN#google")["access_token"]["S"] == "access"
    assert raw(dynamo, "TOKEN#strava") == {}


def test_logs_are_keyed_by_activity(dynamo) -> None:
    store(dynamo).record(LogEntry("889672", Status.UPLOADED, recorded_at=5.0))
    assert raw(dynamo, "LOG#889672")["status"]["S"] == "uploaded"


def test_a_log_entry_carries_a_ttl(dynamo) -> None:
    store(dynamo).record(LogEntry("889672", Status.UPLOADED, recorded_at=1000.0))
    ttl = int(raw(dynamo, "LOG#889672")["ttl"]["N"])
    assert ttl == 1000 + LOG_TTL_DAYS * 86400


def test_the_ttl_window_is_configurable(dynamo) -> None:
    store(dynamo, log_ttl_days=1).record(LogEntry("1", Status.UPLOADED, recorded_at=1000.0))
    assert int(raw(dynamo, "LOG#1")["ttl"]["N"]) == 1000 + 86400


def test_tokens_carry_no_ttl(dynamo) -> None:
    """Expiring them would silently deauthorise the whole pipeline."""
    store(dynamo).save("google", TOKENS, expected_version=0)
    assert "ttl" not in raw(dynamo, "TOKEN#google")


def test_absent_optional_fields_are_not_written_as_empty(dynamo) -> None:
    store(dynamo).record(LogEntry("1", Status.WITHHELD, recorded_at=1.0))
    item = raw(dynamo, "LOG#1")
    assert "strava_activity_id" not in item
    assert "factor" not in item
    assert "reason" not in item


def test_a_float_factor_survives_exactly(dynamo) -> None:
    """DynamoDB numbers are decimal strings; a lossy conversion would be silent."""
    subject = store(dynamo)
    subject.record(LogEntry("1", Status.UPLOADED, factor=0.7228976832, recorded_at=1.0))
    assert subject.get("1").factor == 0.7228976832


def test_an_expiry_float_survives_exactly(dynamo) -> None:
    subject = store(dynamo)
    subject.save("google", Tokens("a", "r", 1756412345.678901), expected_version=0)
    assert subject.load("google").tokens.expires_at == 1756412345.678901


# --- reads ------------------------------------------------------------------


def test_reads_are_strongly_consistent(dynamo, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker reads straight after another's conditional write; stale would lose."""
    seen: list[bool] = []
    original = dynamo.get_item

    def spy(**kwargs):
        seen.append(kwargs.get("ConsistentRead"))
        return original(**kwargs)

    monkeypatch.setattr(dynamo, "get_item", spy)
    store(dynamo).load("google")
    assert seen == [True]


# --- unreadable records -----------------------------------------------------


def test_a_token_record_missing_a_field_is_reported(dynamo) -> None:
    dynamo.put_item(TableName=TABLE, Item={"pk": {"S": "TOKEN#google"}, "access_token": {"S": "a"}})
    with pytest.raises(StoreError, match="token record for google is unreadable"):
        store(dynamo).load("google")


def test_a_log_record_with_an_invented_status_is_reported(dynamo) -> None:
    dynamo.put_item(TableName=TABLE, Item={"pk": {"S": "LOG#1"}, "status": {"S": "invented"}})
    with pytest.raises(StoreError, match="log record for 1 is unreadable"):
        store(dynamo).get("1")


def test_a_log_record_missing_its_status_is_reported(dynamo) -> None:
    dynamo.put_item(TableName=TABLE, Item={"pk": {"S": "LOG#1"}, "reason": {"S": "x"}})
    with pytest.raises(StoreError, match="unreadable"):
        store(dynamo).get("1")


# --- construction -----------------------------------------------------------


def test_the_client_is_built_lazily_not_at_construction(aws_credentials: None) -> None:
    """`boto3.client(...)` at import needs credentials and a region, and breaks CI."""
    subject = DynamoStore(TABLE)
    assert subject._injected is None, "constructing the store must not touch AWS"
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        assert subject.client is not None
        assert subject.client is subject.client, "built once, then reused"


def test_the_default_clock_is_the_real_one(dynamo) -> None:
    subject = DynamoStore(TABLE, client=dynamo)
    subject.record(LogEntry("1", Status.UPLOADED))
    assert subject.get("1").recorded_at == pytest.approx(time.time(), abs=10)
