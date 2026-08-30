"""The local file store: locking, compare-and-swap, permissions, and corruption.

Against a real file in a real temporary directory. The whole point of this
adapter is what it does with the filesystem, so mocking it out would leave
nothing worth testing.
"""

import json
import stat
from pathlib import Path

import pytest

from fakes import Clock
from reckon.clients.oauth import Tokens
from reckon.stores.base import LogEntry, Status, StoreError, TokenConflict, VersionedTokens
from reckon.stores.file import SCHEMA, FileStore

TOKENS = Tokens("access", "refresh", 5000.0)


@pytest.fixture
def store(tmp_path: Path) -> FileStore:
    return FileStore(tmp_path / "nested" / "store.json", now=Clock(now=1000.0).time)


# --- tokens -----------------------------------------------------------------


def test_an_unauthorised_service_loads_as_none(store: FileStore) -> None:
    assert store.load("google") is None


def test_a_saved_pair_round_trips(store: FileStore) -> None:
    saved = store.save("google", TOKENS, expected_version=0)
    assert saved == VersionedTokens(TOKENS, 1)
    assert store.load("google") == VersionedTokens(TOKENS, 1)


def test_the_version_increments_on_every_write(store: FileStore) -> None:
    store.save("google", TOKENS, expected_version=0)
    second = store.save("google", Tokens("next", "refresh", 9000.0), expected_version=1)
    assert second.version == 2
    assert store.load("google").tokens.access_token == "next"


def test_two_services_keep_separate_versions(store: FileStore) -> None:
    store.save("google", TOKENS, expected_version=0)
    store.save("google", TOKENS, expected_version=1)
    store.save("strava", TOKENS, expected_version=0)
    assert store.load("google").version == 2
    assert store.load("strava").version == 1


def test_a_stale_expected_version_is_refused(store: FileStore) -> None:
    """The losing branch of the race in docs/diagrams/token-refresh.puml."""
    store.save("google", TOKENS, expected_version=0)
    with pytest.raises(TokenConflict) as caught:
        store.save("google", Tokens("mine", "refresh", 1.0), expected_version=0)
    assert (caught.value.expected, caught.value.found) == (0, 1)


def test_a_refused_write_leaves_the_winner_intact(store: FileStore) -> None:
    winner = Tokens("winner", "refresh", 9000.0)
    store.save("google", winner, expected_version=0)
    with pytest.raises(TokenConflict):
        store.save("google", Tokens("loser", "refresh", 1.0), expected_version=0)
    assert store.load("google") == VersionedTokens(winner, 1)


def test_writing_a_first_pair_over_an_expected_version_of_one_is_refused(store: FileStore) -> None:
    with pytest.raises(TokenConflict, match="expected version 1, found 0"):
        store.save("google", TOKENS, expected_version=1)


# --- the processed log ------------------------------------------------------


def test_an_unknown_activity_is_none(store: FileStore) -> None:
    assert store.get("12345") is None


def test_a_recorded_entry_round_trips(store: FileStore) -> None:
    entry = LogEntry("12345", Status.UPLOADED, strava_activity_id=999, factor=0.93, recorded_at=7.0)
    store.record(entry)
    assert store.get("12345") == entry


def test_every_status_survives_the_round_trip(store: FileStore) -> None:
    for index, status in enumerate(Status):
        store.record(LogEntry(str(index), status, reason="because", recorded_at=1.0))
    assert [store.get(str(i)).status for i in range(len(Status))] == list(Status)


def test_recording_without_a_timestamp_stamps_it_from_the_clock(store: FileStore) -> None:
    store.record(LogEntry("12345", Status.PASSED_THROUGH, reason="no_gps"))
    assert store.get("12345").recorded_at == 1000.0


def test_re_recording_replaces_the_earlier_decision(store: FileStore) -> None:
    store.record(LogEntry("12345", Status.FAILED, reason="upstream", recorded_at=1.0))
    store.record(LogEntry("12345", Status.UPLOADED, strava_activity_id=42, recorded_at=2.0))
    assert store.get("12345").status is Status.UPLOADED


def test_entries_come_back_oldest_first(store: FileStore) -> None:
    store.record(LogEntry("late", Status.UPLOADED, recorded_at=30.0))
    store.record(LogEntry("early", Status.UPLOADED, recorded_at=10.0))
    store.record(LogEntry("middle", Status.UPLOADED, recorded_at=20.0))
    assert [e.activity_id for e in store.entries()] == ["early", "middle", "late"]


def test_entries_is_empty_on_a_fresh_store(store: FileStore) -> None:
    assert store.entries() == []


# --- status semantics -------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "on_strava"),
    [
        (Status.UPLOADED, True),
        (Status.PASSED_THROUGH, True),
        (Status.WITHHELD, False),
        (Status.FAILED, False),
    ],
)
def test_on_strava_separates_reaching_strava_from_being_corrected(
    status: Status, on_strava: bool
) -> None:
    """Two facts, two axes. Collapsing them is how passed_through got lost once."""
    assert status.on_strava is on_strava


# --- the file itself --------------------------------------------------------


def test_the_file_and_its_parent_are_created_on_demand(store: FileStore) -> None:
    assert not store.path.exists()
    store.save("google", TOKENS, expected_version=0)
    assert store.path.is_file()


def test_the_file_holding_refresh_tokens_is_not_group_or_world_readable(
    store: FileStore,
) -> None:
    store.save("google", TOKENS, expected_version=0)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_loosened_permissions_are_tightened_again_on_the_next_open(store: FileStore) -> None:
    """A stray umask leaving a refresh token world-readable is not hypothetical."""
    store.save("google", TOKENS, expected_version=0)
    store.path.chmod(0o644)
    store.load("google")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_an_empty_file_reads_as_an_empty_store(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    path.write_text("")
    assert FileStore(path).load("google") is None


def test_the_document_is_human_readable(store: FileStore) -> None:
    store.record(LogEntry("12345", Status.UPLOADED, recorded_at=1.0))
    document = json.loads(store.path.read_text())
    assert document["schema"] == SCHEMA
    assert document["logs"]["12345"]["status"] == "uploaded"
    assert "activity_id" not in document["logs"]["12345"], "the key already carries it"


def test_reads_and_writes_interleave_without_losing_data(store: FileStore) -> None:
    store.save("google", TOKENS, expected_version=0)
    store.record(LogEntry("12345", Status.UPLOADED, recorded_at=1.0))
    store.save("strava", TOKENS, expected_version=0)
    assert store.load("google") is not None
    assert store.get("12345") is not None


def test_a_second_store_object_sees_the_first_ones_writes(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    FileStore(path).save("google", TOKENS, expected_version=0)
    assert FileStore(path).load("google") == VersionedTokens(TOKENS, 1)


# --- corruption -------------------------------------------------------------


def test_a_file_that_is_not_json_is_reported_not_ignored(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    path.write_text("{not json")
    with pytest.raises(StoreError, match="not valid JSON"):
        FileStore(path).load("google")


def test_a_json_document_that_is_not_an_object_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(StoreError, match="does not contain a JSON object"):
        FileStore(path).load("google")


def test_an_unknown_schema_is_refused_rather_than_guessed_at(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"schema": 99, "tokens": {}, "logs": {}}))
    with pytest.raises(StoreError, match="re-authorise"):
        FileStore(path).load("google")


def test_missing_sections_default_rather_than_raise(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"schema": SCHEMA}))
    store = FileStore(path)
    assert store.load("google") is None
    assert store.get("1") is None


@pytest.mark.parametrize(
    "record",
    [
        {"access_token": "a", "refresh_token": "r"},
        {"access_token": "a", "refresh_token": "r", "expires_at": "soon", "version": 1},
        "not an object",
    ],
)
def test_an_unreadable_token_record_is_reported(tmp_path: Path, record: object) -> None:
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"schema": SCHEMA, "tokens": {"google": record}, "logs": {}}))
    with pytest.raises(StoreError, match="token record is unreadable"):
        FileStore(path).load("google")


@pytest.mark.parametrize(
    "record",
    [{"reason": "no status"}, {"status": "invented"}, {"status": "uploaded", "recorded_at": "x"}],
)
def test_an_unreadable_log_record_is_reported(tmp_path: Path, record: object) -> None:
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"schema": SCHEMA, "tokens": {}, "logs": {"12345": record}}))
    with pytest.raises(StoreError, match="log record for 12345 is unreadable"):
        FileStore(path).get("12345")
