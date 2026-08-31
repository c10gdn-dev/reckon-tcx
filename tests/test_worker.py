"""The SQS worker: message routing, the delayed re-enqueue, and what gets recorded.

The pipeline is the real one over a `FakeTransport`, so a wrong field name here
shows up rather than being papered over by a fake pipeline. Only the queue is a
list, because that is the boundary being tested.
"""

import json
from typing import Any

import pytest

import builders
from fakes import Clock, FakeLogStore, FakeTransport, RecordingSleep, response
from reckon.aws.worker import (
    FIRST_DELAY_SECONDS,
    MAX_UPLOAD_CHECKS,
    notification_window,
    process_records,
)
from reckon.clients.health import GoogleHealth
from reckon.clients.oauth import TokenHolder, Tokens
from reckon.clients.strava import Strava
from reckon.core.errors import NetworkError, ReckonError
from reckon.pipeline import Pipeline
from reckon.stores.base import Status

CORRECTABLE = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=930.0)
WINDOW = ("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")


class Queue(list):
    def __call__(self, message: dict[str, Any], *, delay_seconds: int = 0) -> None:
        self.append((message, delay_seconds))


def holder() -> TokenHolder:
    return TokenHolder(
        FakeTransport(),
        "https://token.test/",
        client_id="c",
        client_secret="s",
        tokens=Tokens("live", "refresh", 10_000.0),
        now=Clock(now=0.0).time,
    )


def pipeline(health: FakeTransport, strava: FakeTransport, logs: FakeLogStore) -> Pipeline:
    return Pipeline(
        health=GoogleHealth(health, holder(), base_url="https://health.test/v4"),
        strava=Strava(strava, holder(), base_url="https://strava.test/v3"),
        logs=logs,
        now=Clock(now=1000.0).time,
        sleep=RecordingSleep(),
    )


def record(message: dict[str, Any]) -> dict[str, Any]:
    return {"body": json.dumps(message)}


def json_body(payload: Any) -> Any:
    return response(body=json.dumps(payload).encode())


def upload_body(**overrides: Any) -> Any:
    payload = {
        "id": 987,
        "external_id": "1",
        "error": None,
        "status": "processing",
        "activity_id": None,
    }
    payload.update(overrides)
    return json_body(payload)


def run(records, health=None, strava=None, logs=None, queue=None):
    logs = logs if logs is not None else FakeLogStore()
    queue = queue if queue is not None else Queue()
    pipe = pipeline(health or FakeTransport(), strava or FakeTransport(), logs)
    outcomes = process_records(
        records, pipeline=pipe, logs=logs, enqueue=queue, window=lambda _: WINDOW
    )
    return outcomes, logs, queue


# --- routing ----------------------------------------------------------------


def test_an_unknown_message_type_is_raised_not_retried() -> None:
    """Deterministic: it will be unknown next time too, so retrying fills the DLQ."""
    with pytest.raises(ReckonError, match="unknown queue message type"):
        run([record({"type": "invented"})])


def test_a_message_that_is_not_json_is_reported() -> None:
    with pytest.raises(ReckonError, match="queue message is not JSON"):
        run([{"body": "{not json"}])


def test_a_message_that_is_not_an_object_is_reported() -> None:
    with pytest.raises(ReckonError, match="expected an object"):
        run([record([1, 2, 3])])


def test_an_already_decoded_body_is_accepted() -> None:
    """Some invoke paths hand back a dict rather than a string."""
    with pytest.raises(ReckonError, match="unknown queue message type"):
        run([{"body": {"type": "invented"}}])


def test_an_empty_batch_does_nothing() -> None:
    outcomes, logs, queue = run([])
    assert (outcomes, list(logs.recorded), list(queue)) == ([], [], [])


# --- notifications ----------------------------------------------------------


def listing(*ids: str) -> Any:
    return json_body(
        {
            "dataPoints": [
                {
                    "name": f"users/me/dataTypes/exercise/dataPoints/{i}",
                    "exercise": {
                        "interval": {"startTime": "2026-02-15T00:00:00Z"},
                        "exerciseType": "WALKING",
                        "displayName": "Walk",
                    },
                }
                for i in ids
            ]
        }
    )


def test_a_notification_processes_every_activity_in_its_window() -> None:
    health = FakeTransport(listing("1"), response(body=CORRECTABLE))
    strava = FakeTransport(upload_body(activity_id=555))
    outcomes, logs, _ = run(
        [record({"type": "notification", "body": '{"dataType": "exercise"}'})],
        health=health,
        strava=strava,
    )
    assert [o.status for o in outcomes] == [Status.UPLOADED]
    assert logs.recorded[0].strava_activity_id == 555


def test_a_notification_body_that_is_not_json_is_reported() -> None:
    with pytest.raises(ReckonError, match="notification body is not JSON"):
        run([record({"type": "notification", "body": "{oops"})])


def test_a_notification_body_that_is_not_an_object_is_reported() -> None:
    with pytest.raises(ReckonError, match="expected an object"):
        run([record({"type": "notification", "body": "[]"})])


def test_a_transient_fault_propagates_so_sqs_redelivers() -> None:
    """Catching this would delete the message and lose the activity silently."""
    with pytest.raises(NetworkError):
        run(
            [record({"type": "notification", "body": "{}"})],
            health=FakeTransport(NetworkError("reset")),
        )


# --- the notification window ------------------------------------------------


@pytest.mark.parametrize(
    "keys", [("startTime", "endTime"), ("start_time", "end_time"), ("startDateTime", "endDateTime")]
)
def test_every_documented_interval_spelling_is_read(keys: tuple[str, str]) -> None:
    """None has been seen on a real delivery, so all three are tried."""
    start, end = keys
    assert notification_window({"intervals": [{start: "A", end: "B"}]}) == ("A", "B")


def test_several_intervals_collapse_to_their_span() -> None:
    payload = {
        "intervals": [{"startTime": "B", "endTime": "C"}, {"startTime": "A", "endTime": "D"}]
    }
    assert notification_window(payload) == ("A", "D")


def test_unreadable_entries_are_skipped_but_readable_ones_are_used() -> None:
    payload = {"intervals": ["nonsense", {"nope": 1}, {"startTime": "A", "endTime": "B"}]}
    assert notification_window(payload) == ("A", "B")


@pytest.mark.parametrize("payload", [{}, {"intervals": []}, {"intervals": "no"}])
def test_a_notification_with_no_intervals_is_an_error(payload: dict[str, Any]) -> None:
    """Better than silently scanning all of history."""
    with pytest.raises(ReckonError, match="no intervals"):
        notification_window(payload)


def test_intervals_present_but_all_unreadable_is_an_error() -> None:
    with pytest.raises(ReckonError, match="no readable interval"):
        notification_window({"intervals": [{"nope": 1}]})


# --- upload checks ----------------------------------------------------------


def check(attempt: int = 1) -> dict[str, Any]:
    return record(
        {
            "type": "upload_check",
            "strava_upload_id": 987,
            "exercise_id": "889672",
            "attempt": attempt,
        }
    )


def test_a_finished_upload_is_recorded_and_not_re_enqueued() -> None:
    _, logs, queue = run([check()], strava=FakeTransport(upload_body(activity_id=555)))
    assert logs.recorded[0].status is Status.UPLOADED
    assert logs.recorded[0].strava_activity_id == 555
    assert queue == []


def test_a_duplicate_counts_as_uploaded() -> None:
    strava = FakeTransport(upload_body(error="duplicate of activity 42", activity_id=42))
    _, logs, _ = run([check()], strava=strava)
    assert logs.recorded[0].status is Status.UPLOADED
    assert logs.recorded[0].reason == "already on Strava"


def test_a_rejected_upload_is_recorded_as_failed() -> None:
    _, logs, queue = run([check()], strava=FakeTransport(upload_body(error="Invalid file type")))
    assert logs.recorded[0].status is Status.FAILED
    assert logs.recorded[0].reason == "Invalid file type"
    assert queue == []


def test_an_unfinished_upload_is_re_enqueued_delayed_never_slept_on() -> None:
    """A sleeping Lambda is billed wall-clock time (PLAN.md §9)."""
    _, logs, queue = run([check()], strava=FakeTransport(upload_body()))
    ((message, delay),) = queue
    assert message["attempt"] == 2
    assert delay == FIRST_DELAY_SECONDS
    assert logs.recorded == [], "nothing is decided while it is still processing"


@pytest.mark.parametrize(("attempt", "delay"), [(1, 20), (2, 40), (3, 80), (4, 160)])
def test_the_delay_doubles(attempt: int, delay: int) -> None:
    _, _, queue = run([check(attempt)], strava=FakeTransport(upload_body()))
    assert queue[0][1] == delay


def test_the_final_check_gives_up_rather_than_polling_for_ever() -> None:
    """A slow Strava is not a poisoned message and does not belong in the DLQ."""
    _, logs, queue = run([check(MAX_UPLOAD_CHECKS)], strava=FakeTransport(upload_body()))
    assert queue == []
    assert logs.recorded[0].status is Status.FAILED
    assert f"after {MAX_UPLOAD_CHECKS} checks" in logs.recorded[0].reason


def test_the_attempt_defaults_to_one_when_absent() -> None:
    message = record({"type": "upload_check", "strava_upload_id": 987, "exercise_id": "1"})
    _, _, queue = run([message], strava=FakeTransport(upload_body()))
    assert queue[0][0]["attempt"] == 2


def test_the_recorded_time_comes_from_the_pipeline_clock() -> None:
    _, logs, _ = run([check()], strava=FakeTransport(upload_body(activity_id=1)))
    assert logs.recorded[0].recorded_at == 1000.0


def test_a_batch_of_more_than_one_record_is_handled() -> None:
    """batch_size is 1 in production, but nothing here assumes it."""
    strava = FakeTransport(upload_body(activity_id=1), upload_body(activity_id=2))
    _, logs, _ = run([check(), check()], strava=strava)
    assert len(logs.recorded) == 2
