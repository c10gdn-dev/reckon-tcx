"""The two ports the pipeline persists through, and nothing else.

`typing.Protocol` rather than ABCs, deliberately: a `...` body is excluded from
coverage by configuration, so there is no unreachable `raise NotImplementedError`
to explain away later (`PLAN.md` §7).

Both ports are defined here so that `pipeline.py` can be written, tested and run
against `file.py` without DynamoDB, boto3, or an AWS account existing — which is
the whole point of the split in §2.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from reckon.clients.oauth import Tokens
from reckon.core.errors import ReckonError


class StoreError(ReckonError):
    """The store exists but cannot be read. Deterministic — a corrupt file stays
    corrupt, and a retry loop over it is a spin."""


class TokenConflict(ReckonError):
    """A compare-and-swap write lost its race.

    Deterministic, and *not* a failure: the caller re-reads and continues with
    whatever won. Never retry the refresh on this — see `docs/diagrams/
    token-refresh.puml`. It is an exception rather than a return value so that a
    caller cannot use the stale pair by forgetting to check.
    """

    def __init__(self, service: str, expected: int, found: int) -> None:
        self.service = service
        self.expected = expected
        self.found = found
        super().__init__(
            f"{service} tokens changed underneath: expected version {expected}, found {found}"
        )


@dataclass(frozen=True)
class VersionedTokens:
    """A token pair and the version the next write must claim."""

    tokens: Tokens
    version: int


class Status(StrEnum):
    """What became of one activity. Persisted, so the values are the wire format.

    Four, not three. `PLAN.md` §5 originally collapsed the middle two into
    `skipped`, which would have silently dropped every activity Reckon cannot
    improve — yoga, a GPS-less walk, a walk whose lock dropped — when the owner's
    requirement is that Strava record every sport.

    Two independent facts are being recorded, and they are kept apart on purpose:
    `on_strava` says whether the activity got there, `corrected` says whether the
    numbers were improved on the way. Folding them into one value is the mistake
    this project keeps meeting.
    """

    UPLOADED = "uploaded"
    PASSED_THROUGH = "passed_through"
    WITHHELD = "withheld"
    FAILED = "failed"

    @property
    def on_strava(self) -> bool:
        """True when the activity reached Strava, corrected or not."""
        return self in (Status.UPLOADED, Status.PASSED_THROUGH)


@dataclass(frozen=True)
class LogEntry:
    """One activity, and what was decided about it."""

    activity_id: str
    status: Status
    reason: str = ""
    strava_activity_id: int | None = None
    factor: float | None = None
    recorded_at: float = 0.0


class TokenStore(Protocol):
    """Where a service's OAuth pair lives, with a compare-and-swap write."""

    def load(self, service: str) -> VersionedTokens | None:
        """The current pair, or None if this service was never authorised."""
        ...

    def save(self, service: str, tokens: Tokens, *, expected_version: int) -> VersionedTokens:
        """Persist `tokens`, or raise `TokenConflict` if someone else got there first.

        Writes before the caller uses the new access token, so the store never
        holds a pair that has already been spent.
        """
        ...


class ProcessedLogStore(Protocol):
    """Which activities have been dealt with, and how.

    A status and a reason rather than a bare seen-marker: "we uploaded it" and
    "we deliberately did not" are different answers to the next notification
    about the same activity.
    """

    def get(self, activity_id: str) -> LogEntry | None:
        """The recorded decision for this activity, or None if it is new."""
        ...

    def record(self, entry: LogEntry) -> None:
        """Persist one decision. Every recorded decision is final."""
        ...
