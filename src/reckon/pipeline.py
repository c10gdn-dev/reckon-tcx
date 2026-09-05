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

import datetime as dt
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from reckon.clients.health import Exercise, GoogleHealth
from reckon.clients.oauth import TokenHolder, Tokens
from reckon.clients.strava import Strava, Upload
from reckon.core import heartrate, tcx
from reckon.core.errors import AuthError, ReckonError
from reckon.core.rescale import (
    DEFAULT_TOLERANCE,
    MIN_GPS_COVERAGE,
    RescaleResult,
    SkipReason,
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
    "WEIGHTS": "WeightTraining",
    "WORKOUT": "Workout",
}

# Used when `exerciseType` is one this mapping has never seen. Refusing to upload
# is not an option — that is the dropping the whole design forbids — so the only
# question is which wrong answer is least wrong.
#
# It was `Run`, on the reasoning that a wrong-but-plausible type is two taps to
# fix in Strava. The first live run corrected that: the account's `WEIGHTS`
# sessions would have been uploaded as runs, which is not implausible-but-wrong,
# it is a specific false claim about an activity that involved no running at all.
# `Workout` is equally editable and asserts nothing untrue. The warning is still
# what gets the mapping extended.
DEFAULT_SPORT_TYPE = "Workout"

# The link every Reckon upload carries, on its own line under the summary.
PROJECT_URL = "https://github.com/c10gdn-dev/reckon-tcx"

# What a corrected upload's description says, and what an uncorrected one says
# instead. Every activity Reckon touches is identifiable as such, names the
# device that recorded it, and states plainly whether the distance was changed —
# because "Reckon processed this" without saying what it did invites exactly the
# question it was meant to answer.
#
# The wording avoids this project's vocabulary. "Passed through" is precise here
# and meaningless in a Strava feed; "not corrected" is the other way round.
_CORRECTED = "corrected {before} → {after} km"
_NOT_CORRECTED = {
    SkipReason.NO_GPS: "not corrected — no GPS recorded",
    SkipReason.PARTIAL_GPS: "not corrected — GPS incomplete",
    SkipReason.NO_DISTANCE_STREAM: "not corrected — no distance recorded",
}
_NOT_CORRECTED_UNKNOWN = "not corrected — nothing to rescale"


def describe(result: RescaleResult, device: str | None) -> str:
    """The Strava description for an activity Reckon is about to upload."""
    if result.modified:
        summary = _CORRECTED.format(
            before=_km(result.gps_total_m), after=_km(result.result_total_m)
        )
    else:
        reasons = {skip.reason for skip in result.skips}
        summary = (
            _NOT_CORRECTED[next(iter(reasons))] if len(reasons) == 1 else _NOT_CORRECTED_UNKNOWN
        )
    parts = ["Reckon", device, summary] if device else ["Reckon", summary]
    return " · ".join(parts) + "\n" + PROJECT_URL


def _km(metres: float) -> str:
    """Kilometres to two places, as Strava shows them. The unit is added once."""
    return f"{metres / 1000:.2f}"


# Strava's upload is asynchronous. Locally a bounded loop is fine; in Lambda this
# must become a delayed SQS re-enqueue, because a sleeping handler is billed
# wall-clock time (`PLAN.md` §9). Five attempts matches the cap set there.
POLL_ATTEMPTS = 5
POLL_DELAY = 2.0


# Said once per activity, so it is one line and it names the fix rather than
# quoting a 403 body that is the same every time.
_SCOPE_ADVICE = (
    "heart-rate series unavailable (the health_metrics_and_measurements scope is "
    "not granted); the lap average was written instead"
)


def _is_scope_error(exc: AuthError) -> bool:
    return b"scope" in exc.body.lower()


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
    description: str = ""
    # The file this came from, in local mode. Empty when the API supplied it.
    source: str = ""
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
    # The per-second series needs a Restricted scope, which requires an
    # unpublished OAuth client (`health.HEART_RATE_SCOPES`), which costs a
    # weekly re-authorisation. That was declined, so the fetch is off by default
    # and would 403 on every activity if it were not. The lap average, which
    # needs no extra scope, is written regardless.
    merge_heart_rate: bool = False
    heart_rate_tolerance_s: float = heartrate.DEFAULT_TOLERANCE_S

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

    def mark_done(self, *, start_time: str, end_time: str, reason: str) -> list[Outcome]:
        """Record every activity in a window as handled, without fetching or uploading.

        The adoption step for anyone whose activities already reach Strava by
        another route. Without it, a first `sync` re-uploads a history that is
        already there — and Strava's `external_id` deduplication does not save
        you, because whatever put them there first used its own.

        Recorded as `uploaded` because that is what is true: the activity is on
        Strava. The reason records that Reckon was not what put it there.
        """
        outcomes: list[Outcome] = []
        for exercise in self.exercises(start_time, end_time):
            if (known := self.logs.get(exercise.id)) is not None:
                outcomes.append(
                    Outcome(
                        activity_id=known.activity_id,
                        status=known.status,
                        name=exercise.display_name,
                        reason=known.reason,
                        fresh=False,
                    )
                )
                continue
            outcome = Outcome(
                activity_id=exercise.id,
                status=Status.UPLOADED,
                name=exercise.display_name,
                reason=reason,
            )
            self.logs.record(outcome.entry(self.now()))
            outcomes.append(outcome)
        return outcomes

    def local(self, directory: Path) -> list[Outcome]:
        """Correct and upload TCX files exported by hand, from a directory.

        The mode that exists because Google's API export omits heart rate. A file
        exported from the phone app has it; one fetched from
        `:exportExerciseTcx` does not, and Strava's Fitness score is calculated
        from it. So the files are the source of the *track*, and the API is
        consulted only for what the file cannot say.

        What it cannot say is the sport. Real exports carry `Sport="Other"` for
        anything that is not a run or a ride, which the corpus showed is
        information-free — it covers a 5 km walk and a stationary yoga session
        alike. The API's `exerciseType` is what makes a walk upload as a Walk.

        **An existing history entry is overridden**, unlike `sync`, which skips
        one. That is the point: an activity already uploaded from the API, and so
        already on Strava without heart rate, is exactly what this mode is for
        replacing. Delete the Strava copy first — Reckon cannot, and will not,
        delete anything from your account.
        """
        files = sorted(directory.glob("*.tcx"))
        if not files:
            return []

        readings = [(path, *_read(path)) for path in files]
        known = self._activities_covering(started for _, started, _ in readings if started)

        return [self._local_file(path, started, why, known) for path, started, why in readings]

    def _local_file(
        self,
        path: Path,
        started: dt.datetime | None,
        why: str,
        known: Mapping[dt.datetime, Exercise],
    ) -> Outcome:
        """One file: identify it, correct it, upload it, record it."""
        if started is None:
            return _unidentified(path, why)
        exercise = _nearest(known, started)
        if exercise is None:
            # Not "cannot correct", which would still upload — "cannot identify".
            # Without the activity's id there is no external_id for Strava to
            # deduplicate on and no key to record it under, so uploading risks a
            # duplicate that nothing will catch. Reporting it leaves the file in
            # place to be retried once the lookup works.
            return _unidentified(
                path, f"no activity in Google Health starts at {started:%Y-%m-%d %H:%M:%S}"
            )

        outcome = self._decide(exercise, data=path.read_bytes())
        if outcome.fresh and not self.dry_run:
            self.logs.record(outcome.entry(self.now()))
        return replace(outcome, source=path.name)

    def _activities_covering(
        self, moments: Iterable[dt.datetime]
    ) -> Mapping[dt.datetime, Exercise]:
        """Every activity spanning the files' timestamps, keyed by start instant."""
        instants = sorted(moments)
        if not instants:
            return {}
        start = (instants[0] - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (instants[-1] + dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        found: dict[dt.datetime, Exercise] = {}
        for exercise in self.exercises(start, end):
            moment = _instant(exercise.start_time)
            if moment is not None:
                found[moment] = exercise
        return found

    def fetch(self, activity_id: str, *, raw: bool = False) -> bytes:
        """One activity's TCX, corrected unless `raw`. No store, no upload.

        The target is already in the file, so this needs no second request for it
        — the point `PLAN.md` §4 makes about `fetch` and still true on the new API.
        """
        data = self.health.tcx(activity_id)
        return data if raw else self._rescale(data).data

    # --- the decision -------------------------------------------------------

    def _decide(self, exercise: Exercise, *, data: bytes | None = None) -> Outcome:
        # Local mode supplies the bytes; `sync` fetches them. Everything after
        # this point is identical, which is the whole reason the modes share a
        # pipeline rather than a family resemblance.
        data = self.health.tcx(exercise.name) if data is None else data
        data, hr_warnings = self._with_heart_rate(data, exercise)
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
        warnings = (*hr_warnings, *warnings)
        device = _device(data)
        base = Outcome(
            activity_id=exercise.id,
            status=Status.UPLOADED if result.modified else Status.PASSED_THROUGH,
            name=exercise.display_name,
            reason="" if result.modified else _skip_reason(result),
            factor=result.factor if result.modified else None,
            warnings=(*warnings, *result.warnings),
            description=describe(result, device),
        )
        if self.dry_run:
            return replace(base, reason=_prefixed("dry run", base.reason))
        return self._upload(base, result.data, exercise, sport_type)

    def _with_heart_rate(self, data: bytes, exercise: Exercise) -> tuple[bytes, tuple[str, ...]]:
        """Put back the heart rate the API's export leaves out.

        Enrichment, so it never fails an upload. A missing scope, an unreadable
        payload or a network fault costs the trace and not the activity — the
        corrected distance is the point, and arriving without heart rate beats
        not arriving. Anything that goes wrong is reported as a warning.
        """
        data, warnings = self._average_heart_rate(data, exercise)
        if not self.merge_heart_rate:
            return data, warnings
        try:
            samples = self.health.heart_rate(
                start_time=exercise.start_time, end_time=exercise.end_time
            )
        except AuthError as exc:
            # Expected rather than exceptional, and permanent for an account that
            # has not been through Google's verification: the per-second series
            # sits behind a restricted scope. This appears on every activity, so
            # it says the one useful thing rather than quoting the API at length.
            return data, (*warnings, _SCOPE_ADVICE if _is_scope_error(exc) else str(exc))
        except ReckonError as exc:
            return data, (*warnings, f"heart-rate series not merged: {exc}")
        if not samples:
            return data, warnings

        merged = heartrate.merge(data, samples, tolerance_s=self.heart_rate_tolerance_s)
        if merged.matched == 0:
            return data, (
                *warnings,
                f"heart-rate series not merged: {len(samples)} samples, none within "
                f"{self.heart_rate_tolerance_s:g}s of a trackpoint",
            )
        return merged.data, warnings

    def _average_heart_rate(self, data: bytes, exercise: Exercise) -> tuple[bytes, tuple[str, ...]]:
        """Write the summary's average onto the lap, where the file has none.

        Cheap and always available: the activity scope reads it, unlike the
        per-second series. It gives Strava a number rather than a graph, and it
        is the only heart rate an account without the restricted scope will get.
        """
        if exercise.average_heart_rate is None:
            return data, ()
        updated, refused = heartrate.set_average(data, exercise.average_heart_rate)
        return updated, () if refused is None else (refused,)

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
            description=base.description,
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


# How far a file's start timestamp may sit from an activity's and still be the
# same outing. They should agree exactly — both come from the device — but the
# two are written by different exporters and a second of rounding is cheaper to
# tolerate than to debug.
MATCH_TOLERANCE = dt.timedelta(seconds=5)


def _read(path: Path) -> tuple[dt.datetime | None, str]:
    """The instant a file says it started, or None and the reason it could not say.

    The reason is carried rather than reduced to a bare None because the three
    ways this fails — unreadable, not TCX, no `Id` — want three different
    responses from whoever is looking at the directory.
    """
    try:
        started = tcx.started_at(tcx.parse(path.read_bytes()))
    except ReckonError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"cannot read the file: {exc.strerror or exc}"
    if started is None:
        return None, "the file carries no activity Id to match on"
    moment = _instant(started)
    if moment is None:
        return None, f"the activity Id is not a timestamp: {started!r}"
    return moment, ""


def _instant(text: str) -> dt.datetime | None:
    """An RFC 3339 timestamp as an absolute instant, whatever offset it carries.

    A file exported from the app writes local time with an offset
    (`2026-09-05T14:13:36.000+01:00`); the API reports the same moment as UTC
    (`2026-09-05T13:13:36Z`). Comparing the strings would never match.
    """
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _nearest(known: Mapping[dt.datetime, Exercise], started: dt.datetime) -> Exercise | None:
    for moment, exercise in known.items():
        if abs(moment - started) <= MATCH_TOLERANCE:
            return exercise
    return None


def _unidentified(path: Path, why: str) -> Outcome:
    return Outcome(
        activity_id="",
        status=Status.WITHHELD,
        name=path.name,
        reason=f"not uploaded: {why}",
        source=path.name,
    )


def _device(data: bytes) -> str | None:
    """The model that recorded the file, for the upload description."""
    for activity in tcx.activities(tcx.parse(data)):
        name = tcx.creator_name(activity)
        if name:
            return name
    return None


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
