"""Exception hierarchy for the pure layer.

Every error raised by `core` is a `ReckonError`, so callers — the CLI now, the
pipeline in phase 5 — have exactly one thing to catch at the boundary.
"""


class ReckonError(Exception):
    """Base class for every failure Reckon raises deliberately.

    One thing to catch at the boundary. Whether retrying would help is a
    *separate* axis, carried by the `Transient` marker below rather than by
    position in this hierarchy — see the note there.
    """


class Transient:
    """Marker for a failure that a later identical attempt might survive.

    Deliberately a mixin rather than a base class or a `retryable` flag. "What
    went wrong" and "would doing it again help" are independent questions, and
    this codebase has already been bitten four times by one mechanism standing
    in for two meanings (see the `Lap`/`Trackpoint` distance split, and
    `skipped` versus `withheld`). A 429 and a dropped connection are nothing
    alike as faults but identical as decisions; a 429 and a 404 are the reverse.

    Catch `Transient` to decide about retrying, and the concrete class to decide
    what to tell the user. `PLAN.md` §5 turns on exactly this: transient faults
    propagate so SQS retries and the DLQ alarm eventually fires, deterministic
    ones are recorded and never retried.
    """


class MalformedTCX(ReckonError):
    """The input is not a TCX document we can read."""


class MissingTarget(ReckonError):
    """No target distance was given and the file carries none of its own.

    Raised only when the caller asked Reckon to take the target from the file.
    An explicit target is never second-guessed.
    """


class ToleranceExceeded(ReckonError):
    """The rescale factor is further from 1 than the caller allowed.

    The bound is asymmetric. A factor below 1 means the GPS stream over-measured,
    which is ordinary jitter and can legitimately be large, so `tolerance` gives
    it a loose floor. A factor above 1 means the stream measured short, which
    jitter cannot cause — when the target came from the file that is partial GPS
    and is reported as such, so reaching this exception from above means an
    explicit target that is too large.
    """

    def __init__(
        self, factor: float, gps_total_m: float, target_m: float, tolerance: float
    ) -> None:
        self.factor = factor
        self.gps_total_m = gps_total_m
        self.target_m = target_m
        self.tolerance = tolerance
        super().__init__(
            f"factor {factor:.4f} is outside tolerance {tolerance:.4f} "
            f"(GPS total {gps_total_m:.1f} m, target {target_m:.1f} m); "
            f"check the target distance, or pass --on-tolerance clamp|proceed"
        )


class NetworkError(Transient, ReckonError):
    """The request never produced a response: DNS, refused, reset, timed out."""


class HTTPError(ReckonError):
    """A response arrived carrying a status outside 2xx.

    Deterministic by default — a 404 is a 404 however many times you ask. The
    two status ranges where that is not true get their own `Transient`
    subclasses below.
    """

    def __init__(self, status: int, method: str, url: str, body: bytes) -> None:
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        super().__init__(f"{method} {url} returned {status}{_excerpt(body)}")


class AuthError(HTTPError):
    """401 or 403. Deterministic: the same credentials will fail identically.

    Recovering means refreshing a token or re-running the OAuth flow, which is
    the caller's decision to make, so this is never retried in place.
    """


class RateLimited(Transient, HTTPError):
    """429. Carries `retry_after` in seconds when the server named one."""

    def __init__(
        self, status: int, method: str, url: str, body: bytes, retry_after: float | None
    ) -> None:
        self.retry_after = retry_after
        super().__init__(status, method, url, body)


class ServerError(Transient, HTTPError):
    """5xx. The request was plausibly fine; the far side was not."""


# Enough of a failing body to identify the fault in a log line, and no more.
# Error bodies are occasionally an entire HTML page, and these messages end up
# in CloudWatch where volume costs money.
_BODY_EXCERPT = 200


def _excerpt(body: bytes) -> str:
    text = " ".join(body.decode("utf-8", errors="replace").split())
    if not text:
        return ""
    if len(text) > _BODY_EXCERPT:
        text = text[:_BODY_EXCERPT] + "..."
    return f": {text}"
