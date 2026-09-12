"""The three ports the pipeline persists through, and nothing else.

`typing.Protocol` rather than ABCs, deliberately: a `...` body is excluded from
coverage by configuration, so there is no unreachable `raise NotImplementedError`
to explain away later (`PLAN.md` §7).

All three are defined here so that `pipeline.py` can be written, tested and run
against `file.py` without DynamoDB, boto3, or an AWS account existing — which is
the whole point of the split in §2.
"""

import datetime as dt
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


class MatchKind(StrEnum):
    """How we know Strava has an activity — or that we do not know.

    Not a boolean, because *how* we know decides how far to trust it. Reducing
    these four to "on Strava: yes/no" is how a duplicate gets uploaded or a real
    activity gets silently skipped (`PLAN.md` §13.2).
    """

    EXTERNAL_ID = "external_id"
    """Certain. Reckon uploaded it, and Strava echoed back the id Reckon sent."""

    START_TIME = "start_time"
    """An inference. Something on Strava starts when this activity does — very
    likely the same outing arriving by another route, but nothing proves it."""

    AMBIGUOUS = "ambiguous"
    """Two or more Strava activities are within tolerance. **Nothing is assumed**:
    catch-up skips these and a person decides."""

    NONE = "none"
    """Looked, and found nothing. Distinct from never having looked, which is
    `checked_at == 0.0` — the two are the same `MatchKind` and must not be the
    same answer to "should catch-up consider this"."""


@dataclass(frozen=True)
class InventoryEntry:
    """One activity Google Health holds, and whether Strava has it.

    **A fact about the world, not a decision Reckon made.** `LogEntry` records
    the decision; this records what there was to decide about. The codebase
    conflated the two once: `Pipeline.mark_done` wrote a `LogEntry` with status
    `uploaded` and the reason "already on Strava before Reckon" — a fact wearing
    a decision's clothes — which is why *known, never processed* could not be
    expressed at all. It was deleted when `reconcile` replaced it with an answer
    obtained by asking Strava rather than assumed. See `ARCHITECTURE.md`.
    """

    activity_id: str
    start_time: str
    end_time: str = ""
    exercise_type: str = ""
    display_name: str = ""
    distance_m: float | None = None
    seen_at: float = 0.0
    strava_activity_id: int | None = None
    match: MatchKind = MatchKind.NONE
    checked_at: float = 0.0


def chronological(start_time: str) -> str:
    """An RFC 3339 timestamp normalised so lexicographic order is chronological.

    Both adapters need to return a window oldest-first, and both would otherwise
    compare the raw strings — which breaks the moment two activities are written
    with different UTC offsets, the same trap local-mode matching hit. Shared
    here rather than implemented twice, because two copies of this would drift.

    An unparseable value is returned unchanged. `clients/health.py` deliberately
    yields an activity whose timestamp it cannot read rather than dropping it, so
    one can reach the store; it will sort by its raw text and may fall outside
    any window, which is visible and wrong rather than invisible and wrong.
    """
    try:
        return dt.datetime.fromisoformat(start_time).astimezone(dt.UTC).strftime(_CHRONO)
    except ValueError:
        return start_time


_CHRONO = "%Y-%m-%dT%H:%M:%SZ"


class InventoryStore(Protocol):
    """What Google Health holds, and what Strava already has of it.

    The third port, added for deployed mode. Unlike `ProcessedLogStore`, whose
    entries are final, these records are rewritten: backfill discovers them and
    reconcile updates them, repeatedly and idempotently.
    """

    def put(self, entry: InventoryEntry) -> None:
        """Write one activity's record, replacing any earlier one."""
        ...

    def inventory(self, activity_id: str) -> InventoryEntry | None:
        """One activity's record, or None if backfill has never seen it."""
        ...

    def between(self, start_time: str, end_time: str) -> list[InventoryEntry]:
        """Every activity starting in `[start_time, end_time)`, oldest first."""
        ...
