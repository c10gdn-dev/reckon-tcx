"""Activity in, Strava out. The half of Reckon that both run modes share.

`PLAN.md` §2: everything from "here is an activity id" to "Strava has it" is this
file, and only storage and trigger differ between the local CLI and Lambda. So
this imports the store *protocols*, never a concrete store, and never boto3 —
`test_layering.py` enforces that rather than trusting it.

The outcome model is the load-bearing part. Four results, and the difference
between the middle two is the one this project keeps having to relearn:

- `uploaded` — corrected, and Strava has it.
- `passed_through` — could not be corrected, and Strava has it anyway. Yoga, an
  indoor walk, a track whose GPS dropped out. **Not a failure and not a skip.**
- `withheld` — deliberately not sent: a malformed file, or a factor outside
  tolerance under `abort`.
- a raised exception — transient. Network, 429, 5xx. Nothing is recorded, so SQS
  redelivers and the DLQ alarm eventually fires.

Only the first three are ever written to the log store, and every one of them is
final. If it is in the store, it is never done again.
"""

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from reckon.clients.health import Exercise, GoogleHealth
from reckon.clients.oauth import TokenHolder, Tokens
from reckon.clients.strava import Strava, Upload
from reckon.core.errors import ReckonError
from reckon.core.rescale import (
    DEFAULT_TOLERANCE,
    MIN_GPS_COVERAGE,
    RescaleResult,
    ToleranceAction,
    rescale_tcx,
)
from reckon.stores.base import (
    LogEntry,
    ProcessedLogStore,
    Status,
    StoreError,
    TokenConflict,
    TokenStore,
)

# Google Health's vocabulary to Strava's. Lives here rather than in either
# client because neither service should have to know the other exists
# (`PLAN.md` §12).
#
# This mapping is why the ingest change matters beyond the hostname: the corpus
# showed TCX `Sport` is information-free — `Other` covers both a 5.26 km walk and
# a stationary yoga session — so the type has to come from the API summary. It
# always did; Google's `exerciseType` is simply a better-labelled source for it
# than Fitbit's activity name was.
SPORT_TYPES: Mapping[str, str] = {
    "RUNNING": "Run",
    "WALKING": "Walk",
    "BIKING": "Ride",
    "HIKING": "Hike",
    "SWIMMING_OPEN_WATER": "Swim",
    "YOGA": "Yoga",
    "WORKOUT": "Workout",
}

# Used when `exerciseType` is one this mapping has never seen. `Run` rather than
# `Workout` because a wrong-but-plausible type is editable in Strava's UI in two
# taps, and the alternative — refusing to upload — is the dropping that the whole
# design says never to do. The warning is what gets the mapping extended.
DEFAULT_SPORT_TYPE = "Run"

# Strava's upload is asynchronous. Locally a bounded loop is fine; in Lambda this
# must become a delayed SQS re-enqueue, because a sleeping handler is billed
# wall-clock time (`PLAN.md` §9). Five attempts matches the cap set there.
POLL_ATTEMPTS = 5
POLL_DELAY = 2.0


class NotAuthorised(ReckonError):
    """No token pair is stored for this service. Deterministic until a human acts."""

    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(f"no {service} tokens stored; run `python scripts/authorize.py {service}`")


@dataclass(frozen=True)
class Outcome:
    """What happened to one activity, and enough to explain it in one line."""

    activity_id: str
    status: Status
    name: str = ""
    reason: str = ""
    strava_activity_id: int | None = None
    factor: float | None = None
    warnings: tuple[str, ...] = ()
    # False when this is a stored decision being replayed rather than made. A
    # separate field, not a status: "what was decided" and "was it decided just
    # now" are independent, and merging them is how `passed_through` got lost the
    # first time.
    fresh: bool = True

    def entry(self, recorded_at: float) -> LogEntry:
        return LogEntry(
            activity_id=self.activity_id,
            status=self.status,
            reason=self.reason,
            strava_activity_id=self.strava_activity_id,
            factor=self.factor,
            recorded_at=recorded_at,
        )


def token_holder(
    store: TokenStore,
    service: str,
    *,
    transport: Callable[..., object],
    token_url: str,
    client_id: str,
    client_secret: str,
    now: Callable[[], float] = time.time,
) -> TokenHolder:
    """A `TokenHolder` whose refreshes are persisted compare-and-swap.

    The closure holds the version, so `clients/` never learns that a store
    exists. On a lost race it re-reads and returns the winner's pair, which the
    holder then adopts — the rule drawn out in `docs/diagrams/token-refresh.puml`.
    The refresh is never retried: a conflict means the work was already done.
    """
    stored = store.load(service)
    if stored is None:
        raise NotAuthorised(service)
    state = {"version": stored.version}

    def persist(tokens: Tokens) -> Tokens:
        try:
            saved = store.save(service, tokens, expected_version=state["version"])
        except TokenConflict:
            winner = store.load(service)
            if winner is None:
                raise StoreError(f"{service} tokens vanished mid-refresh") from None
            state["version"] = winner.version
            return winner.tokens
        state["version"] = saved.version
        return tokens

    return TokenHolder(
        transport,  # type: ignore[arg-type]
        token_url,
        client_id=client_id,
        client_secret=client_secret,
        tokens=stored.tokens,
        now=now,
        on_refresh=persist,
    )


@dataclass
class Pipeline:
    """One activity id to one `Outcome`, and a window of them to a list."""

    health: GoogleHealth
    strava: Strava
    logs: ProcessedLogStore
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    tolerance: float = DEFAULT_TOLERANCE
    on_tolerance: ToleranceAction = ToleranceAction.ABORT
    min_gps_coverage: float = MIN_GPS_COVERAGE
    poll_attempts: int = POLL_ATTEMPTS
    poll_delay: float = POLL_DELAY
    dry_run: bool = False
    sport_types: Mapping[str, str] = field(default_factory=lambda: SPORT_TYPES)

    def sync(self, *, start_time: str, end_time: str) -> list[Outcome]:
        """Process every activity starting in the window, oldest first."""
        return [self.process(exercise) for exercise in self.exercises(start_time, end_time)]

    def exercises(self, start_time: str, end_time: str) -> Iterable[Exercise]:
        return self.health.exercises(start_time=start_time, end_time=end_time)

    def process(self, exercise: Exercise) -> Outcome:
        """Decide about one activity, and record the decision if it is new."""
        if (known := self.logs.get(exercise.id)) is not None:
            return Outcome(
                activity_id=known.activity_id,
                status=known.status,
                name=exercise.display_name,
                reason=known.reason,
                strava_activity_id=known.strava_activity_id,
                factor=known.factor,
                fresh=False,
            )

        outcome = self._decide(exercise)
        if outcome.fresh and not self.dry_run:
            self.logs.record(outcome.entry(self.now()))
        return outcome

    def fetch(self, activity_id: str, *, raw: bool = False) -> bytes:
        """One activity's TCX, corrected unless `raw`. No store, no upload.

        The target is already in the file, so this needs no second request for it
        — the point `PLAN.md` §4 makes about `fetch` and still true on the new API.
        """
        data = self.health.tcx(activity_id)
        return data if raw else self._rescale(data).data

    # --- the decision -------------------------------------------------------

    def _decide(self, exercise: Exercise) -> Outcome:
        data = self.health.tcx(exercise.name)
        try:
            result = self._rescale(data)
        except ReckonError as exc:
            return Outcome(
                activity_id=exercise.id,
                status=Status.WITHHELD,
                name=exercise.display_name,
                reason=str(exc),
            )

        sport_type, warnings = self._sport_type(exercise)
        base = Outcome(
            activity_id=exercise.id,
            status=Status.UPLOADED if result.modified else Status.PASSED_THROUGH,
            name=exercise.display_name,
            reason="" if result.modified else _skip_reason(result),
            factor=result.factor if result.modified else None,
            warnings=(*warnings, *result.warnings),
        )
        if self.dry_run:
            return replace(base, reason=_prefixed("dry run", base.reason))
        return self._upload(base, result.data, exercise, sport_type)

    def _rescale(self, data: bytes) -> RescaleResult:
        return rescale_tcx(
            data,
            tolerance=self.tolerance,
            on_tolerance=self.on_tolerance,
            min_gps_coverage=self.min_gps_coverage,
        )

    def _sport_type(self, exercise: Exercise) -> tuple[str, tuple[str, ...]]:
        mapped = self.sport_types.get(exercise.exercise_type)
        if mapped is not None:
            return mapped, ()
        return DEFAULT_SPORT_TYPE, (
            f"unmapped exerciseType {exercise.exercise_type!r}; uploaded as {DEFAULT_SPORT_TYPE}",
        )

    def _upload(self, base: Outcome, data: bytes, exercise: Exercise, sport_type: str) -> Outcome:
        upload = self.strava.upload(
            data,
            name=exercise.display_name or "Activity",
            external_id=exercise.id,
            sport_type=sport_type,
            description="Distance corrected by Reckon." if base.factor else "",
        )
        return self._settle(base, self._await(upload))

    def _await(self, upload: Upload) -> Upload:
        """Poll until Strava finishes, or until the attempts run out."""
        for attempt in range(1, self.poll_attempts + 1):
            if upload.done:
                return upload
            self.sleep(self.poll_delay * 2 ** (attempt - 1))
            upload = self.strava.upload_status(upload.id)
        return upload

    def _settle(self, base: Outcome, upload: Upload) -> Outcome:
        if upload.duplicate:
            # Strava dedupes on external_id. The activity is there, which is the
            # thing that matters; that Reckon's own store had not recorded it is
            # a store problem, not a reason to try again.
            return replace(base, reason=_prefixed("already on Strava", base.reason))
        if upload.error is not None:
            return replace(base, status=Status.FAILED, reason=upload.error)
        if upload.activity_id is None:
            return replace(
                base,
                status=Status.FAILED,
                reason=f"still processing after {self.poll_attempts} checks: {upload.status}",
            )
        return replace(base, strava_activity_id=upload.activity_id)


def _skip_reason(result: RescaleResult) -> str:
    """Why an unmodified file was passed through, in the file's own words."""
    if not result.skips:
        return "nothing to rescale"
    return ", ".join(sorted({str(skip.reason) for skip in result.skips}))


def _prefixed(prefix: str, reason: str) -> str:
    return f"{prefix}: {reason}" if reason else prefix


def summarise(outcomes: Sequence[Outcome]) -> Mapping[str, int]:
    """Counts by status, for the one-line report `reckon sync` ends on."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        key = "already done" if not outcome.fresh else str(outcome.status)
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "DEFAULT_SPORT_TYPE",
    "POLL_ATTEMPTS",
    "SPORT_TYPES",
    "NotAuthorised",
    "Outcome",
    "Pipeline",
    "summarise",
    "token_holder",
]
