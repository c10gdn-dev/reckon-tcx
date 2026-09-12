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
from reckon.stores.base import (
    InventoryEntry,
    LogEntry,
    MatchKind,
    Status,
    TokenConflict,
    VersionedTokens,
)
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


# --- inventory --------------------------------------------------------------
#
# The third port. Unlike the processed log, whose entries are final, these are
# rewritten: backfill discovers an activity and reconcile updates it, repeatedly.


def entry(
    activity_id: str = "1", start_time: str = "2026-02-23T13:10:00Z", **kwargs
) -> InventoryEntry:
    return InventoryEntry(activity_id=activity_id, start_time=start_time, **kwargs)


def test_an_unknown_activity_has_no_inventory_record(store) -> None:
    assert store.inventory("nothing") is None


def test_an_inventory_record_round_trips_every_field(store) -> None:
    full = InventoryEntry(
        activity_id="889672",
        start_time="2026-02-23T13:10:00Z",
        end_time="2026-02-23T13:25:00Z",
        exercise_type="WALKING",
        display_name="Morning Walk",
        distance_m=930.5,
        seen_at=1000.0,
        strava_activity_id=55,
        match=MatchKind.EXTERNAL_ID,
        checked_at=2000.0,
    )

    store.put(full)

    assert store.inventory("889672") == full


def test_the_optional_fields_survive_being_absent(store) -> None:
    """A backfilled record has no Strava match yet, and no distance for yoga."""
    store.put(entry("889672"))

    found = store.inventory("889672")
    assert found.distance_m is None
    assert found.strava_activity_id is None
    assert found.match is MatchKind.NONE
    assert found.checked_at == 0.0


def test_putting_the_same_activity_again_replaces_it(store) -> None:
    """Reconcile rewrites what backfill wrote. Not a conflict, not an append."""
    store.put(entry("889672", seen_at=1000.0))
    store.put(entry("889672", seen_at=1000.0, strava_activity_id=55, match=MatchKind.START_TIME))

    found = store.inventory("889672")
    assert found.match is MatchKind.START_TIME
    assert found.strava_activity_id == 55


def test_seen_at_defaults_to_now_when_unset(store) -> None:
    """Both adapters stamp it, so backfill does not have to carry a clock."""
    store.put(entry("889672"))

    assert store.inventory("889672").seen_at == 1000.0


def test_between_returns_the_window_oldest_first(store) -> None:
    for i, moment in enumerate(
        ("2026-02-23T15:00:00Z", "2026-02-23T09:00:00Z", "2026-02-23T12:00:00Z")
    ):
        store.put(entry(str(i), start_time=moment))

    found = store.between("2026-02-23T00:00:00Z", "2026-02-24T00:00:00Z")

    assert [e.start_time for e in found] == [
        "2026-02-23T09:00:00Z",
        "2026-02-23T12:00:00Z",
        "2026-02-23T15:00:00Z",
    ]


def test_between_is_half_open(store) -> None:
    """Inclusive at the start, exclusive at the end, so adjacent windows tile.

    DynamoDB's BETWEEN is inclusive at both ends, so the upper bound is trimmed
    by the adapter. A boundary the two stores disagreed on would surface as a
    duplicate upload at midnight and nowhere else.
    """
    store.put(entry("low", start_time="2026-02-23T00:00:00Z"))
    store.put(entry("high", start_time="2026-02-24T00:00:00Z"))

    found = store.between("2026-02-23T00:00:00Z", "2026-02-24T00:00:00Z")

    assert [e.activity_id for e in found] == ["low"]


def test_between_orders_on_the_instant_not_the_text(store) -> None:
    """Two offsets, one ordering. Comparing the raw strings reverses these."""
    store.put(entry("later", start_time="2026-02-23T14:00:00+01:00"))  # 13:00Z
    store.put(entry("earlier", start_time="2026-02-23T12:30:00Z"))

    found = store.between("2026-02-23T00:00:00Z", "2026-02-24T00:00:00Z")

    assert [e.activity_id for e in found] == ["earlier", "later"]


def test_between_windows_on_the_instant_too(store) -> None:
    """An offset timestamp must fall in the window its instant belongs to."""
    store.put(entry("evening", start_time="2026-02-24T00:30:00+01:00"))  # 23:30Z on the 23rd

    assert [
        e.activity_id for e in store.between("2026-02-23T00:00:00Z", "2026-02-24T00:00:00Z")
    ] == ["evening"]


def test_between_over_an_empty_window_is_empty(store) -> None:
    store.put(entry("1", start_time="2026-02-23T13:10:00Z"))

    assert store.between("2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z") == []


def test_inventory_and_log_are_separate_records(store) -> None:
    """The whole reason this port exists: a fact and a decision, kept apart.

    An activity can be known and undecided, which no `Status` can express.
    """
    store.put(entry("889672"))

    assert store.inventory("889672") is not None
    assert store.get("889672") is None

    store.record(LogEntry("889672", Status.UPLOADED))

    assert store.inventory("889672") is not None
    assert store.get("889672").status is Status.UPLOADED


def test_a_timestamp_that_cannot_be_parsed_is_kept_as_it_came(store) -> None:
    """`clients/health.py` yields an activity whose timestamp it cannot read
    rather than dropping it, so one can reach the store. It sorts by its raw
    text and may fall outside every window — visibly wrong, rather than absent.
    """
    store.put(entry("odd", start_time="whenever"))

    assert store.inventory("odd").start_time == "whenever"
    assert store.between("2026-02-23T00:00:00Z", "2026-02-24T00:00:00Z") == []


def test_when_access_was_granted_survives_a_round_trip(store) -> None:
    """The seven-day clock is measured against this, so losing it loses the warning."""
    granted = Tokens("access", "refresh", 5000.0, authorised_at=1234.0)

    store.save("google", granted, expected_version=0)

    assert store.load("google").tokens.authorised_at == 1234.0
