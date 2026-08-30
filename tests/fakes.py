"""Test doubles for everything Reckon injects rather than reaches for.

There is no mock library here and no patching, deliberately: the production code
takes its transport, clock, sleep and RNG as parameters, so a test supplies real
objects that happen to be boring (`PLAN.md` §7).
"""

import random

from reckon.clients.http import Request, Response


def response(
    status: int = 200,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    url: str = "https://example.test/",
) -> Response:
    """A `Response` with everything but the interesting field defaulted."""
    return Response(status=status, headers=headers or {}, body=body, url=url)


class FakeTransport:
    """Replays canned outcomes in order and records what it was asked for.

    An entry may be a `Response` to return or an exception to raise, which is
    what makes a "two timeouts then a 200" retry test a one-liner.
    """

    def __init__(self, *outcomes: Response | BaseException) -> None:
        self.outcomes: list[Response | BaseException] = list(outcomes)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def calls(self) -> int:
        return len(self.requests)


class Clock:
    """A clock that only moves when a test moves it."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self._now = now

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class RecordingSleep:
    """A sleep that records its argument and returns immediately.

    Optionally advances a `Clock`, so code that both waits and then reads the
    time sees a consistent story.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)


class MaxJitter(random.Random):
    """Full-jitter backoff with the jitter pinned to the top of its window.

    Lets a test assert the exact backoff sequence without asserting a range,
    while still going through the same `rng.uniform` call production uses.
    """

    def uniform(self, a: float, b: float) -> float:
        return b


class FakeTokenStore:
    """An in-memory `TokenStore` with the same compare-and-swap semantics.

    Not a stand-in for `FileStore` — that has its own tests against a real file.
    This exists so a pipeline test can stage a lost race without a filesystem.
    """

    def __init__(self, **initial: object) -> None:
        from reckon.stores.base import VersionedTokens

        self.records: dict[str, VersionedTokens] = {
            service: VersionedTokens(tokens, 1)  # type: ignore[arg-type]
            for service, tokens in initial.items()
        }
        self.saves: list[str] = []

    def load(self, service: str) -> object:
        return self.records.get(service)

    def save(self, service: str, tokens: object, *, expected_version: int) -> object:
        from reckon.stores.base import TokenConflict, VersionedTokens

        self.saves.append(service)
        current = self.records.get(service)
        found = 0 if current is None else current.version
        if found != expected_version:
            raise TokenConflict(service, expected_version, found)
        saved = VersionedTokens(tokens, expected_version + 1)  # type: ignore[arg-type]
        self.records[service] = saved
        return saved


class FakeLogStore:
    """An in-memory `ProcessedLogStore` that also records the order of writes."""

    def __init__(self, *entries: object) -> None:
        self.entries: dict[str, object] = {e.activity_id: e for e in entries}  # type: ignore[attr-defined]
        self.recorded: list[object] = []

    def get(self, activity_id: str) -> object:
        return self.entries.get(activity_id)

    def record(self, entry: object) -> None:
        self.entries[entry.activity_id] = entry  # type: ignore[attr-defined]
        self.recorded.append(entry)
