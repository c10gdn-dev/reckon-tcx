"""The two stores must be indistinguishable to `pipeline.py`.

`stores/file.py` and `stores/dynamo.py` implement the same two ports, and the
whole local/AWS split in `PLAN.md` §2 rests on the pipeline being unable to tell
which it has. Testing each separately would let them drift apart in ways no test
names; this runs one set of behaviours against both.

Adapter-specific concerns — file permissions, locking, condition expressions —
stay in `test_stores.py` and `test_dynamo.py`.
"""

from pathlib import Path

import pytest

from conftest import TABLE
from fakes import Clock
from reckon.clients.oauth import Tokens
from reckon.stores.base import LogEntry, Status, TokenConflict, VersionedTokens
from reckon.stores.dynamo import DynamoStore
from reckon.stores.file import FileStore

TOKENS = Tokens("access", "refresh", 5000.0)


@pytest.fixture(params=["file", "dynamo"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    """The same contract, once per implementation."""
    clock = Clock(now=1000.0)
    if request.param == "file":
        return FileStore(tmp_path / "store.json", now=clock.time)
    return DynamoStore(TABLE, client=request.getfixturevalue("dynamo"), now=clock.time)


# --- tokens -----------------------------------------------------------------


def test_an_unauthorised_service_loads_as_none(store) -> None:
    assert store.load("google") is None


def test_a_saved_pair_round_trips(store) -> None:
    assert store.save("google", TOKENS, expected_version=0) == VersionedTokens(TOKENS, 1)
    assert store.load("google") == VersionedTokens(TOKENS, 1)


def test_the_version_increments_on_every_write(store) -> None:
    store.save("google", TOKENS, expected_version=0)
    second = Tokens("next", "refresh", 9000.0)
    assert store.save("google", second, expected_version=1).version == 2
    assert store.load("google") == VersionedTokens(second, 2)


def test_two_services_keep_separate_versions(store) -> None:
    store.save("google", TOKENS, expected_version=0)
    store.save("google", TOKENS, expected_version=1)
    store.save("strava", TOKENS, expected_version=0)
    assert store.load("google").version == 2
    assert store.load("strava").version == 1


def test_a_stale_expected_version_is_refused(store) -> None:
    """The losing branch of docs/diagrams/token-refresh.puml."""
    store.save("google", TOKENS, expected_version=0)
    with pytest.raises(TokenConflict) as caught:
        store.save("google", Tokens("mine", "refresh", 1.0), expected_version=0)
    assert (caught.value.expected, caught.value.found) == (0, 1)


def test_a_refused_write_leaves_the_winner_intact(store) -> None:
    winner = Tokens("winner", "refresh", 9000.0)
    store.save("google", winner, expected_version=0)
    with pytest.raises(TokenConflict):
        store.save("google", Tokens("loser", "refresh", 1.0), expected_version=0)
    assert store.load("google") == VersionedTokens(winner, 1)


def test_a_first_write_claiming_version_one_is_refused(store) -> None:
    """Version 0 means "no record yet", which is not the same as "version is 0"."""
    with pytest.raises(TokenConflict, match="expected version 1, found 0"):
        store.save("google", TOKENS, expected_version=1)


# --- the processed log ------------------------------------------------------


def test_an_unknown_activity_is_none(store) -> None:
    assert store.get("12345") is None


def test_a_recorded_entry_round_trips(store) -> None:
    entry = LogEntry("12345", Status.UPLOADED, "why", 999, 0.93, 7.0)
    store.record(entry)
    assert store.get("12345") == entry


@pytest.mark.parametrize("status", list(Status))
def test_every_status_survives_the_round_trip(store, status: Status) -> None:
    store.record(LogEntry("12345", status, reason="because", recorded_at=1.0))
    assert store.get("12345").status is status


def test_a_minimal_entry_round_trips(store) -> None:
    """Optional fields absent, not empty."""
    store.record(LogEntry("12345", Status.WITHHELD, recorded_at=3.0))
    assert store.get("12345") == LogEntry("12345", Status.WITHHELD, recorded_at=3.0)


def test_recording_without_a_timestamp_stamps_it_from_the_clock(store) -> None:
    store.record(LogEntry("12345", Status.PASSED_THROUGH, reason="no_gps"))
    assert store.get("12345").recorded_at == 1000.0


def test_re_recording_replaces_the_earlier_decision(store) -> None:
    store.record(LogEntry("12345", Status.FAILED, reason="upstream", recorded_at=1.0))
    store.record(LogEntry("12345", Status.UPLOADED, strava_activity_id=42, recorded_at=2.0))
    assert store.get("12345").status is Status.UPLOADED


def test_a_log_and_a_token_do_not_collide(store) -> None:
    """One table, one file, two key spaces."""
    store.save("google", TOKENS, expected_version=0)
    store.record(LogEntry("google", Status.UPLOADED, recorded_at=1.0))
    assert store.load("google") == VersionedTokens(TOKENS, 1)
    assert store.get("google").status is Status.UPLOADED
