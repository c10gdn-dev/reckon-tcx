"""The one place in Reckon that touches the network.

`fitbit.py` and `strava.py` take a `Transport` at construction and never import
urllib themselves, so above this module every test injects canned responses and
no test needs a mock library or a monkeypatched `urlopen` (`PLAN.md` §7).

The seam is split in two on purpose. `send` performs exactly one request and
reports what came back, including a 500; `retrying` wraps any transport and adds
the decisions — which statuses are worth repeating, how long to wait, when to
give up and raise. Keeping those apart is what lets the retry logic be tested
against a dictionary and `send` be tested against a real socket.
"""

import http.client
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from reckon.core.errors import (
    AuthError,
    HTTPError,
    NetworkError,
    RateLimited,
    ServerError,
    Transient,
)

DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Request:
    """One HTTP request. Frozen so a retry cannot replay a mutated version."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class Response:
    """One HTTP response, fully read.

    The body is bytes because half of what Reckon fetches is a TCX file. Header
    names are lowercased on the way in, since HTTP treats them case-insensitively
    and Strava and Google do not agree on capitalisation.
    """

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        """Decode the body as JSON. Raises `ValueError` if it is not."""
        return json.loads(self.body)


# The seam. Anything matching this shape is a transport: `send`, the retrying
# wrapper around it, or a test fake reading from a list.
Transport = Callable[[Request], Response]


def send(request: Request) -> Response:
    """Perform one request and return whatever came back.

    A non-2xx status is a `Response`, not an exception — classifying it is
    `retrying`'s job. Only the absence of a response raises here.
    """
    raw_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with urllib.request.urlopen(raw_request, timeout=request.timeout) as raw:
            return _read(raw, raw.status)
    except urllib.error.HTTPError as exc:
        # urllib raises 4xx and 5xx rather than returning them, but the exception
        # *is* the response object, so it reads back exactly the same way.
        with exc:
            return _read(exc, exc.code)
    except TimeoutError as exc:
        # Checked before URLError, which it is unrelated to but often carries:
        # a timeout while *connecting* arrives wrapped as URLError(TimeoutError)
        # and lands below, while one while *reading* the body surfaces bare.
        raise NetworkError(
            f"{request.method} {request.url}: timed out after {request.timeout:g}s"
        ) from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"{request.method} {request.url}: {exc.reason}") from exc
    except (http.client.HTTPException, OSError) as exc:
        # urllib wraps most connection faults in URLError, but not all of them:
        # a server that closes the socket before writing a status line raises
        # `RemoteDisconnected` straight through. Same fault, same decision.
        raise NetworkError(f"{request.method} {request.url}: {exc!r}") from exc


def _read(raw: Any, status: int) -> Response:
    return Response(
        status=status,
        headers={key.lower(): value for key, value in raw.headers.items()},
        body=raw.read(),
        url=raw.url,
    )


@dataclass(frozen=True)
class RetryPolicy:
    """How hard to try, and how long to wait between attempts.

    `sleep` and `rng` are injected rather than reached for globally so the retry
    branches are exercised instantly and deterministically (`PLAN.md` §7). The
    defaults are the real ones, so production code constructs `RetryPolicy()`.
    """

    attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 20.0
    # Both APIs are rate limited per hour, so a Retry-After can legitimately be
    # minutes. Past this it is better to fail and let SQS redeliver later than
    # to hold a Lambda open — billed wall-clock time with a hard ceiling.
    max_retry_after: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"attempts must be at least 1, got {self.attempts}")


def retrying(transport: Transport, policy: RetryPolicy | None = None) -> Transport:
    """Wrap a transport so transient faults are retried and the rest raise.

    The returned transport yields only 2xx responses. Everything else has been
    turned into the typed error for its cause, which is what lets the pipeline
    route on `Transient` rather than on status codes.
    """
    policy = RetryPolicy() if policy is None else policy

    def call(request: Request) -> Response:
        attempt = 1
        while True:
            final = attempt >= policy.attempts
            try:
                response = transport(request)
            except NetworkError:
                if final:
                    raise
                delay = _backoff(policy, attempt)
            else:
                if 200 <= response.status < 300:
                    return response
                error = classify(request, response)
                if final or not isinstance(error, Transient):
                    raise error
                delay = _delay_for(policy, error, attempt)
            policy.sleep(delay)
            attempt += 1

    return call


def classify(request: Request, response: Response) -> HTTPError:
    """Turn a non-2xx response into the error that describes why it failed."""
    status = response.status
    if status == 429:
        return RateLimited(
            status, request.method, request.url, response.body, retry_after(response)
        )
    if status >= 500:
        return ServerError(status, request.method, request.url, response.body)
    if status in (401, 403):
        return AuthError(status, request.method, request.url, response.body)
    return HTTPError(status, request.method, request.url, response.body)


def retry_after(response: Response) -> float | None:
    """`Retry-After` in seconds, or None when absent or not a plain number.

    RFC 9110 also permits an HTTP-date, which would need a clock to interpret and
    a decision about clock skew. Neither Strava nor Google Health has been
    observed sending one, so the date form falls back to ordinary backoff instead
    of dragging a clock into this module.
    """
    raw = response.header("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _delay_for(policy: RetryPolicy, error: HTTPError, attempt: int) -> float:
    """Honour a server-named delay, else back off.

    A `Retry-After` is not jittered: the server named a time, and spreading a
    fleet out matters far less than not asking again before it said to.
    """
    named = getattr(error, "retry_after", None)
    if named is None:
        return _backoff(policy, attempt)
    return min(named, policy.max_retry_after)


def _backoff(policy: RetryPolicy, attempt: int) -> float:
    """Exponential backoff with full jitter.

    Jitter spans the whole window rather than a band around the target, which is
    what actually decorrelates two workers that failed on the same second.
    """
    window = min(policy.max_delay, policy.base_delay * 2 ** (attempt - 1))
    return policy.rng.uniform(0.0, window)
