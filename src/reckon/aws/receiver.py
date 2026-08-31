"""The webhook endpoint. Answers Google, enqueues, and does nothing else.

Two jobs, both fast, because Google treats a slow or non-204 reply as a failed
delivery and starts backing off.

**Verification handshake** (`PLAN.md` §8). On creating or updating a subscriber,
Google sends two probes: one carrying the configured `Authorization` header,
which must be answered 200 or 201, and one carrying none, which must be answered
401 or 403. A subscriber that answers the second one 200 is rejected, so the
unauthenticated path being a *failure* is the feature, not an oversight.

**Authentication is the shared secret, not the signature.** Google signs with
ECDSA P-256 over a rotating keyset, which the standard library cannot verify, and
adding a crypto dependency would cost the zero-dependency property the whole
deployment rests on (§3). The configured `Authorization` header is compared with
`hmac.compare_digest` instead. That is weaker, and it is sound only while this
handler trusts nothing in the body: it copies the body to a queue, and the worker
re-fetches everything from the API. **If the worker ever starts believing the
notification's contents, this decision has to be revisited.**
"""

import datetime as dt
import hmac
import json
from collections.abc import Callable, Mapping
from typing import Any

# Google reads 204 specifically as "delivered" and releases any backlog. Any
# other 2xx is not documented to do that, so it is not used.
DELIVERED = 204

# The handshake wants a plain success for the authorised probe. 200 rather than
# 204 because the probe is a request for confirmation, not a delivery.
VERIFIED = 200

UNAUTHORISED = 401


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda Function URL entry point. Wired by Terraform in phase 7."""
    import os

    from reckon.aws.queue import Sqs

    return receive(
        event,
        secret=os.environ["RECKON_WEBHOOK_SECRET"],
        enqueue=Sqs(os.environ["RECKON_QUEUE_URL"]).send,
    )


def receive(
    event: Mapping[str, Any],
    *,
    secret: str,
    enqueue: Callable[[Mapping[str, Any]], None],
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(tz=dt.UTC),
) -> dict[str, Any]:
    """Authenticate, enqueue, acknowledge.

    Split from `handler` so every branch is testable with a dictionary, which is
    what keeps §7's "thin handlers" promise honest.
    """
    if not _authorised(event, secret):
        # Also the correct answer to the handshake's unauthenticated probe: a
        # subscriber that answers it 200 is rejected by Google.
        return _response(UNAUTHORISED)

    body = _body(event)
    if _is_verification(body):
        return _response(VERIFIED)

    enqueue(
        {
            "type": "notification",
            "received_at": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Verbatim, as a string. The worker re-fetches from the API and never
            # trusts this, but keeping the exact bytes makes a failed delivery
            # diagnosable from the queue alone.
            "body": body,
        }
    )
    return _response(DELIVERED)


def _authorised(event: Mapping[str, Any], secret: str) -> bool:
    """Constant-time comparison of the configured header.

    Header names arrive lowercased through a Function URL, but a direct invoke or
    a test harness may not, so they are folded rather than assumed.
    """
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("authorization")
    if not isinstance(supplied, str) or not secret:
        return False
    return hmac.compare_digest(supplied, secret)


def _body(event: Mapping[str, Any]) -> str:
    raw = event.get("body")
    if not isinstance(raw, str):
        return ""
    if event.get("isBase64Encoded"):
        import base64

        return base64.b64decode(raw).decode("utf-8", errors="replace")
    return raw


def _is_verification(body: str) -> bool:
    """Google's probe carries `{"type": "verification"}` and nothing to process."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "verification"


def _response(status: int) -> dict[str, Any]:
    return {"statusCode": status, "body": ""}
