"""The webhook endpoint: authentication, the handshake, and the enqueue.

Every branch is reachable with a dictionary, which is what `PLAN.md` §7's "thin
handlers" is for. No AWS is involved — `receive` takes the enqueue as a callable.
"""

import base64
import datetime as dt
import json
from typing import Any

from reckon.aws.receiver import DELIVERED, UNAUTHORISED, VERIFIED, receive

SECRET = "Bearer s3cret-value"


class Enqueued(list):
    """Records what would have gone to the queue."""

    def __call__(self, message: dict[str, Any]) -> None:
        self.append(message)


def event(*, body: str = "{}", auth: str | None = SECRET, **extra: Any) -> dict[str, Any]:
    headers = {} if auth is None else {"authorization": auth}
    return {"headers": headers, "body": body, **extra}


def call(**kwargs: Any) -> tuple[dict[str, Any], Enqueued]:
    queue = Enqueued()
    moment = dt.datetime(2026, 8, 31, 20, 4, 59, tzinfo=dt.UTC)
    return receive(event(**kwargs), secret=SECRET, enqueue=queue, now=lambda: moment), queue


# --- authentication ---------------------------------------------------------


def test_a_correct_secret_is_accepted() -> None:
    response, queue = call(body='{"dataType": "exercise"}')
    assert response["statusCode"] == DELIVERED
    assert len(queue) == 1


def test_a_wrong_secret_is_refused_and_enqueues_nothing() -> None:
    response, queue = call(auth="Bearer wrong")
    assert response["statusCode"] == UNAUTHORISED
    assert queue == []


def test_a_missing_header_is_refused() -> None:
    response, queue = call(auth=None)
    assert response["statusCode"] == UNAUTHORISED
    assert queue == []


def test_the_unauthenticated_probe_must_fail_for_the_handshake_to_pass() -> None:
    """Google rejects a subscriber that answers its unauthenticated probe 200."""
    response, _ = call(auth=None, body='{"type": "verification"}')
    assert response["statusCode"] == UNAUTHORISED


def test_header_case_does_not_matter() -> None:
    """Function URLs lowercase them; a direct invoke or a test harness may not."""
    raw = {"headers": {"Authorization": SECRET}, "body": "{}"}
    assert receive(raw, secret=SECRET, enqueue=Enqueued())["statusCode"] == DELIVERED


def test_a_non_string_header_is_refused() -> None:
    response = receive(
        {"headers": {"authorization": 7}, "body": "{}"}, secret=SECRET, enqueue=Enqueued()
    )
    assert response["statusCode"] == UNAUTHORISED


def test_an_empty_configured_secret_refuses_everything() -> None:
    """Otherwise a misconfigured function would accept an empty header."""
    response = receive(event(auth=""), secret="", enqueue=Enqueued())
    assert response["statusCode"] == UNAUTHORISED


def test_missing_headers_entirely_is_refused() -> None:
    assert receive({"body": "{}"}, secret=SECRET, enqueue=Enqueued())["statusCode"] == UNAUTHORISED


# --- the verification handshake ---------------------------------------------


def test_the_authorised_probe_is_answered_with_success() -> None:
    response, queue = call(body='{"type": "verification"}')
    assert response["statusCode"] == VERIFIED
    assert queue == [], "a probe is not a delivery"


def test_a_real_notification_is_not_mistaken_for_a_probe() -> None:
    response, queue = call(body='{"dataType": "exercise", "operation": "UPSERT"}')
    assert response["statusCode"] == DELIVERED
    assert len(queue) == 1


# --- what reaches the queue -------------------------------------------------


def test_the_message_matches_the_documented_shape() -> None:
    body = '{"dataType": "exercise"}'
    _, queue = call(body=body)
    assert queue[0] == {
        "type": "notification",
        "received_at": "2026-08-31T20:04:59Z",
        "body": body,
    }


def test_the_body_is_forwarded_verbatim() -> None:
    """Kept exactly so a failed delivery is diagnosable from the queue alone."""
    body = '{"weird":  true,\\n "spacing": 1}'
    _, queue = call(body=body)
    assert queue[0]["body"] == body


def test_a_base64_body_is_decoded() -> None:
    body = '{"dataType": "exercise"}'
    _, queue = call(body=base64.b64encode(body.encode()).decode(), isBase64Encoded=True)
    assert queue[0]["body"] == body


def test_a_missing_body_still_acknowledges() -> None:
    """Never leave Google retrying over something this handler cannot use."""
    response = receive({"headers": {"authorization": SECRET}}, secret=SECRET, enqueue=Enqueued())
    assert response["statusCode"] == DELIVERED


def test_an_unparseable_body_is_enqueued_rather_than_refused() -> None:
    """The worker decides what a body means; the receiver only proves it arrived."""
    response, queue = call(body="not json at all")
    assert response["statusCode"] == DELIVERED
    assert queue[0]["body"] == "not json at all"


def test_a_json_body_that_is_not_an_object_is_not_a_probe() -> None:
    response, queue = call(body="[1, 2, 3]")
    assert response["statusCode"] == DELIVERED
    assert len(queue) == 1


def test_the_acknowledgement_is_204_specifically() -> None:
    """Google reads 204 as delivered and releases the backlog; other 2xx are not documented to."""
    assert DELIVERED == 204
    response, _ = call(body='{"dataType": "exercise"}')
    assert response["statusCode"] == 204


def test_the_default_clock_stamps_something_plausible() -> None:
    queue = Enqueued()
    receive(event(body='{"a": 1}'), secret=SECRET, enqueue=queue)
    assert queue[0]["received_at"].endswith("Z")
    assert json.loads('"' + queue[0]["received_at"] + '"')
