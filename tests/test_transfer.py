"""Moving a store's contents into another store.

Run in both directions against the real adapters, because direction-agnosticism
is the point: both satisfy the same ports, so the same function serves a
laptop-to-AWS migration and an AWS-to-laptop debugging copy.
"""

from pathlib import Path

import pytest

from conftest import TABLE
from fakes import Clock
from reckon.clients.oauth import Tokens
from reckon.stores.base import LogEntry, Status, VersionedTokens
from reckon.stores.dynamo import DynamoStore
from reckon.stores.file import FileStore
from reckon.stores.transfer import SERVICES, copy_logs, copy_tokens

GOOGLE = Tokens("google-access", "google-refresh", 5000.0)
STRAVA = Tokens("strava-access", "strava-refresh", 6000.0)


@pytest.fixture
def pair(tmp_path: Path, dynamo):
    """A file store and a DynamoDB store, both empty."""
    clock = Clock(now=1000.0)
    return (
        FileStore(tmp_path / "store.json", now=clock.time),
        DynamoStore(TABLE, client=dynamo, now=clock.time),
    )


# --- tokens -----------------------------------------------------------------


def test_both_services_are_copied(pair) -> None:
    source, destination = pair
    source.save("google", GOOGLE, expected_version=0)
    source.save("strava", STRAVA, expected_version=0)

    result = copy_tokens(source, destination)

    assert result.copied == ("google", "strava")
    assert destination.load("google") == VersionedTokens(GOOGLE, 1)
    assert destination.load("strava") == VersionedTokens(STRAVA, 1)


def test_it_works_in_the_other_direction_too(pair) -> None:
    """Both adapters satisfy the same ports, so debugging a deployment is free."""
    file_store, dynamo_store = pair
    dynamo_store.save("google", GOOGLE, expected_version=0)

    assert copy_tokens(dynamo_store, file_store).copied == ("google",)
    assert file_store.load("google") == VersionedTokens(GOOGLE, 1)


def test_a_service_the_source_lacks_is_reported_not_failed(pair) -> None:
    source, destination = pair
    source.save("google", GOOGLE, expected_version=0)

    result = copy_tokens(source, destination)

    assert result.copied == ("google",)
    assert any("strava: nothing to copy" in w for w in result.warnings)
    assert destination.load("strava") is None


def test_an_occupied_destination_is_left_alone_by_default(pair) -> None:
    """It may be *ahead*: a running worker refreshes on its own schedule."""
    source, destination = pair
    source.save("google", GOOGLE, expected_version=0)
    newer = Tokens("newer-access", "google-refresh", 99_000.0)
    destination.save("google", newer, expected_version=0)

    result = copy_tokens(source, destination)

    assert result.copied == ()
    assert result.skipped == ("google",)
    assert destination.load("google").tokens == newer
    assert any("--overwrite" in w for w in result.warnings)


def test_overwrite_replaces_and_advances_the_version(pair) -> None:
    source, destination = pair
    source.save("google", GOOGLE, expected_version=0)
    destination.save("google", Tokens("old", "old", 1.0), expected_version=0)

    result = copy_tokens(source, destination, overwrite=True)

    assert result.copied == ("google",)
    assert destination.load("google") == VersionedTokens(GOOGLE, 2)


def test_the_services_copied_can_be_narrowed(pair) -> None:
    source, destination = pair
    source.save("google", GOOGLE, expected_version=0)
    source.save("strava", STRAVA, expected_version=0)

    assert copy_tokens(source, destination, services=["strava"]).copied == ("strava",)
    assert destination.load("google") is None


def test_copying_nothing_at_all_is_reported_as_empty(pair) -> None:
    source, destination = pair
    result = copy_tokens(source, destination)
    assert result.empty is True
    assert len(result.warnings) == len(SERVICES)


def test_a_copy_that_did_something_is_not_empty(pair) -> None:
    source, destination = pair
    source.save("google", GOOGLE, expected_version=0)
    assert copy_tokens(source, destination).empty is False


# --- the processed log ------------------------------------------------------


def test_log_entries_are_copied_so_activities_stay_done(pair) -> None:
    """Otherwise the first notification re-processes everything already synced."""
    source, destination = pair
    entries = [
        LogEntry("1", Status.UPLOADED, strava_activity_id=11, factor=0.93, recorded_at=1.0),
        LogEntry("2", Status.PASSED_THROUGH, reason="no_gps", recorded_at=2.0),
        LogEntry("3", Status.WITHHELD, reason="malformed", recorded_at=3.0),
    ]
    for entry in entries:
        source.record(entry)

    assert copy_logs(source.entries(), destination) == 3
    assert [destination.get(str(i)) for i in (1, 2, 3)] == entries


def test_copying_no_entries_is_zero(pair) -> None:
    _, destination = pair
    assert copy_logs([], destination) == 0


def test_a_copied_log_stops_the_pipeline_reprocessing(pair) -> None:
    """The point of copying them at all."""
    source, destination = pair
    source.record(LogEntry("889672", Status.UPLOADED, strava_activity_id=42, recorded_at=1.0))
    copy_logs(source.entries(), destination)
    assert destination.get("889672").status is Status.UPLOADED
