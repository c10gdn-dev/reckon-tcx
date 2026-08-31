"""The SQS handler. Discriminates on message type and delegates to the pipeline.

Two shapes on the queue (`PLAN.md` §9), and the worker does nothing a local
`reckon sync` does not — the pipeline is identical code. What differs is only
what happens to a slow Strava upload: locally a bounded loop waits for it, here
the message is re-enqueued with `DelaySeconds`, because a sleeping Lambda is
billed wall-clock time.

Transient faults are allowed to propagate. That is the whole contract with SQS:
an exception means the message is redelivered and, if it keeps failing, lands in
the DLQ where an alarm fires. A caught-and-logged exception would delete the
message and lose the activity silently.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from reckon.core.errors import ReckonError
from reckon.pipeline import Outcome, Pipeline
from reckon.stores.base import LogEntry, ProcessedLogStore, Status

# Matches the cap in `PLAN.md` §9. On the fifth check with the upload still
# processing, the log is marked failed and polling stops — a slow Strava is not
# a poisoned message and does not belong in the DLQ.
MAX_UPLOAD_CHECKS = 5

# 20, 40, 80, 160 seconds. Doubling, and never a sleep.
FIRST_DELAY_SECONDS = 20


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda SQS entry point. Wired by Terraform in phase 7."""
    from reckon.aws.config import build_pipeline, from_environment
    from reckon.aws.queue import Sqs

    pipeline = build_pipeline()
    outcomes = process_records(
        event.get("Records") or [],
        pipeline=pipeline,
        logs=pipeline.logs,
        enqueue=Sqs(from_environment("RECKON_QUEUE_URL")).send,
        window=notification_window,
    )
    return {"processed": len(outcomes)}


def notification_window(payload: Mapping[str, Any]) -> tuple[str, str]:
    """The interval a notification covers.

    Google's notification carries `intervals`, unlike Fitbit's, so the worker can
    query the window directly instead of listing a whole day and diffing. The
    field has several documented time formats and none has yet been observed on
    a real delivery, so the shapes are tried in turn and a plain absence is an
    error rather than a silent full-history scan.
    """
    intervals = payload.get("intervals")
    if not isinstance(intervals, list) or not intervals:
        raise ReckonError(f"notification carries no intervals: {sorted(payload)}")

    starts: list[str] = []
    ends: list[str] = []
    for interval in intervals:
        if not isinstance(interval, dict):
            continue
        start, end = _endpoints(interval)
        if start and end:
            starts.append(start)
            ends.append(end)
    if not starts:
        raise ReckonError(f"no readable interval in notification: {intervals!r}")
    return min(starts), max(ends)


def _endpoints(interval: Mapping[str, Any]) -> tuple[str, str]:
    """Start and end of one interval, whichever spelling it arrived in."""
    for start_key, end_key in (
        ("startTime", "endTime"),
        ("start_time", "end_time"),
        ("startDateTime", "endDateTime"),
    ):
        start, end = interval.get(start_key), interval.get(end_key)
        if isinstance(start, str) and isinstance(end, str):
            return start, end
    return "", ""


def process_records(
    records: Sequence[Mapping[str, Any]],
    *,
    pipeline: Pipeline,
    logs: ProcessedLogStore,
    enqueue: Callable[..., None],
    window: Callable[[Mapping[str, Any]], tuple[str, str]],
) -> list[Outcome]:
    """Handle one SQS batch. `batch_size` is 1, so normally one record.

    `window` turns a notification body into the interval to query, which is a
    decision about Google's payload rather than about queue plumbing, so it is
    injected rather than assumed here.
    """
    outcomes: list[Outcome] = []
    for record in records:
        message = _message(record)
        kind = message.get("type")
        if kind == "notification":
            outcomes.extend(_notification(message, pipeline=pipeline, window=window))
        elif kind == "upload_check":
            _upload_check(message, pipeline=pipeline, logs=logs, enqueue=enqueue)
        else:
            # Deterministic: an unknown shape will still be unknown next time, so
            # retrying it only fills the DLQ. Loud, because it means a producer
            # and this consumer disagree.
            raise ReckonError(f"unknown queue message type {kind!r}")
    return outcomes


def _notification(
    message: Mapping[str, Any],
    *,
    pipeline: Pipeline,
    window: Callable[[Mapping[str, Any]], tuple[str, str]],
) -> list[Outcome]:
    """Fetch the activities the notification covers and process each.

    The body is parsed only to find the time range. Nothing else in it is
    trusted, which is what makes the receiver's shared-secret authentication
    sufficient — everything that matters is re-fetched from the API.
    """
    body = message.get("body")
    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError as exc:
        raise ReckonError(f"notification body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReckonError(f"notification body is {type(payload).__name__}, expected an object")

    start_time, end_time = window(payload)
    return pipeline.sync(start_time=start_time, end_time=end_time)


def _upload_check(
    message: Mapping[str, Any],
    *,
    pipeline: Pipeline,
    logs: ProcessedLogStore,
    enqueue: Callable[..., None],
) -> None:
    """Poll one Strava upload, and either settle it or re-enqueue delayed."""
    upload_id = int(message["strava_upload_id"])
    activity_id = str(message["exercise_id"])
    attempt = int(message.get("attempt", 1))

    upload = pipeline.strava.upload_status(upload_id)
    if upload.done:
        logs.record(_settled(activity_id, upload, pipeline.now()))
        return

    if attempt >= MAX_UPLOAD_CHECKS:
        logs.record(
            LogEntry(
                activity_id=activity_id,
                status=Status.FAILED,
                reason=f"still processing after {MAX_UPLOAD_CHECKS} checks: {upload.status}",
                recorded_at=pipeline.now(),
            )
        )
        return

    enqueue(
        {**message, "attempt": attempt + 1},
        delay_seconds=FIRST_DELAY_SECONDS * 2 ** (attempt - 1),
    )


def _settled(activity_id: str, upload: Any, recorded_at: float) -> LogEntry:
    if upload.duplicate:
        # Strava dedupes on external_id. The activity is there, which is the
        # thing that matters; that this store had not recorded it is a store
        # problem, not a reason to upload again.
        return LogEntry(
            activity_id=activity_id,
            status=Status.UPLOADED,
            reason="already on Strava",
            strava_activity_id=upload.activity_id,
            recorded_at=recorded_at,
        )
    if upload.error is not None:
        return LogEntry(
            activity_id=activity_id,
            status=Status.FAILED,
            reason=upload.error,
            recorded_at=recorded_at,
        )
    return LogEntry(
        activity_id=activity_id,
        status=Status.UPLOADED,
        strava_activity_id=upload.activity_id,
        recorded_at=recorded_at,
    )


def _message(record: Mapping[str, Any]) -> Mapping[str, Any]:
    body = record.get("body")
    try:
        message = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError as exc:
        raise ReckonError(f"queue message is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ReckonError(f"queue message is {type(message).__name__}, expected an object")
    return message
