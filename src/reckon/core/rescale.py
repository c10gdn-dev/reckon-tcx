"""The transform: rescale a TCX distance stream to a known total.

Pure. No I/O, no clock, no network. Given bytes and a target distance it returns
bytes and the numbers that describe what it did.

Distance and speed values are multiplied by a single factor. Nothing else is
touched — in particular every time-bearing element round-trips byte-identical,
which is the invariant `test_rescale.py` pins down.
"""

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum

from reckon.core import tcx
from reckon.core.errors import MissingTarget, ToleranceExceeded

# How far *below* 1 the factor may fall. Deliberately asymmetric — see the guard
# in `rescale_tcx` — because the two directions mean different things.
#
# A factor below 1 means the GPS stream over-measured, which is ordinary jitter.
# The corpus has one real walk at 0.723: a short meandering route with 91.6% GPS
# coverage and the highest wiggle measured, so genuinely noisy rather than
# broken. An earlier symmetric 0.2 refused it, which was a false refusal on the
# activity with the most to correct. 0.4 admits it with headroom while still
# catching a target that is wrong by an order of magnitude, such as metres
# supplied where kilometres were meant.
DEFAULT_TOLERANCE = 0.4

# Minimum fraction of an activity's elapsed time that must carry a GPS fix
# before its distance stream is considered a complete record of the route.
#
# Calibrated against the corpus: nine complete tracks measure 89.2%-99.4%, and
# the one activity that lost lock measures 71.7%. The default sits between those
# clusters. Be aware that no real file has yet been observed between 72% and 89%,
# so the exact threshold is a judgement inside an unobserved gap — it is exposed
# as a parameter for that reason.
MIN_GPS_COVERAGE = 0.80

# A factor above this means the GPS stream measured *less* than the activity's
# own total. GPS jitter is strictly additive — every wobble lengthens the path,
# none shortens it — so a complete track cannot come in short. When the target
# was taken from the file, that is evidence of missing track rather than of
# noise. All nine complete tracks in the corpus fall below 1.0; the highest is
# 0.9943. The margin above 1.0 absorbs rounding in the two recorded totals.
MAX_CREDIBLE_FACTOR = 1.005

# Number of decimal places kept when writing a scaled value back. Distances are
# metres and speeds are m/s; seven places is far beyond the precision of either
# measurement, so this only exists to stop float repr from writing 17 digits.
_PRECISION = 7


class SkipReason(StrEnum):
    """Why an activity was left unscaled.

    Structured rather than prose because phase 5 has to route on it: every one of
    these is a deterministic pass-through, never a transient fault, and none of
    them is a reason to withhold the upload.
    """

    NO_GPS = "no_gps"
    NO_DISTANCE_STREAM = "no_distance_stream"
    PARTIAL_GPS = "partial_gps"


@dataclass(frozen=True)
class Skip:
    """One activity that was left exactly as it was found, and why."""

    activity: str
    reason: SkipReason
    detail: str


class ToleranceAction(StrEnum):
    """What to do when the factor is further from 1 than the caller allowed."""

    ABORT = "abort"
    CLAMP = "clamp"
    PROCEED = "proceed"


# Distance-derived speeds. Matching on tag name across the activity subtree
# cannot touch a timestamp by construction, which is the point.
#
# `DistanceMeters` is deliberately absent: the two places it occurs mean
# different things and take different factors. `Trackpoint/DistanceMeters` is the
# GPS stream being corrected; `Lap/DistanceMeters` is Fitbit's own total, i.e.
# the target itself. Scaling the target by the factor derived *from* it destroys
# the ground truth and makes the transform non-idempotent — a second run would
# shrink the file again. `_scale` handles the two separately.
_SCALED_SPEED_TAGS = frozenset(
    {
        tcx.qn(tcx.TCX_NS, "MaximumSpeed"),
        tcx.qn(tcx.AX_NS, "Speed"),
        tcx.qn(tcx.AX_NS, "AvgSpeed"),
    }
)


@dataclass(frozen=True)
class RescaleResult:
    """The rescaled document plus everything a caller needs to report on it."""

    data: bytes
    gps_total_m: float
    target_m: float | None
    factor: float
    trackpoint_count: int
    warnings: tuple[str, ...]
    modified: bool
    skips: tuple[Skip, ...] = ()

    @property
    def result_total_m(self) -> float:
        """The distance total the output file now carries."""
        return self.gps_total_m * self.factor


def rescale_tcx(
    tcx_bytes: bytes,
    target_distance_m: float | None = None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    on_tolerance: ToleranceAction = ToleranceAction.ABORT,
    min_gps_coverage: float = MIN_GPS_COVERAGE,
) -> RescaleResult:
    """Rescale the distance stream in `tcx_bytes` so its total is `target_distance_m`.

    When `target_distance_m` is None the target is taken from the file's own
    `Lap/DistanceMeters` — Fitbit's stride-fused total, which the calibration
    corpus shows is what Google Health displays. Pass a number to override it.

    Activities with no GPS, and activities with no distance stream, are left
    exactly as they were rather than having one fabricated for them. That case
    resolves before any target is needed, so a file with nothing to scale — a
    yoga session, say — round-trips byte-identically whether or not a target was
    supplied or could be found.
    """
    if target_distance_m is not None and (
        not math.isfinite(target_distance_m) or target_distance_m <= 0
    ):
        raise ValueError(
            f"target distance must be a positive number of metres, got {target_distance_m!r}"
        )

    root = tcx.parse(tcx_bytes)
    warnings: list[str] = []
    skips: list[Skip] = []
    scalable: list[ET.Element] = []
    gps_total = 0.0
    trackpoint_count = 0

    for activity in tcx.activities(root):
        name = tcx.label(activity)
        # Counted for every activity, scaled or skipped: this is what the file
        # holds, not what the transform touched, and it is what gets reported.
        final, non_monotonic, count = _distance_stream(activity)
        trackpoint_count += count
        if not tcx.has_position(activity):
            _skip(skips, warnings, name, SkipReason.NO_GPS, "no GPS positions (indoor?)")
            continue
        if final is None:
            _skip(
                skips, warnings, name, SkipReason.NO_DISTANCE_STREAM, "no Trackpoint/DistanceMeters"
            )
            continue
        coverage = tcx.gps_coverage(activity)
        if coverage < min_gps_coverage:
            # The stream describes less ground than was actually covered, so
            # scaling it would spread the missing distance over the part of the
            # route that *was* recorded. Refuse regardless of how plausible the
            # resulting factor looks.
            _skip(
                skips,
                warnings,
                name,
                SkipReason.PARTIAL_GPS,
                f"GPS covers only {coverage:.1%} of the elapsed time "
                f"(needs {min_gps_coverage:.0%}); the distance stream is incomplete",
            )
            continue
        if non_monotonic:
            # Multiplication preserves monotonicity, so this is worth saying but
            # not worth stopping for.
            warnings.append(f"activity {name}: distance stream is not monotonic")
        scalable.append(activity)
        gps_total += final

    if not scalable:
        warnings.append("no activity carries a usable GPS distance stream; returned unchanged")
        return _unchanged(tcx_bytes, target_distance_m, trackpoint_count, warnings, skips)
    if gps_total == 0.0:
        warnings.append("GPS distance total is zero; returned unchanged")
        return _unchanged(tcx_bytes, target_distance_m, trackpoint_count, warnings, skips)

    lap_totals = {id(a): tcx.lap_distance_total(a) for a in scalable}
    from_file = target_distance_m is None
    if from_file:
        target_distance_m = _target_from_file(scalable, lap_totals)

    factor = target_distance_m / gps_total
    if from_file and factor > MAX_CREDIBLE_FACTOR:
        # Jitter cannot make a track measure short, so the file's own total
        # exceeding the GPS sum means part of the route went unrecorded — a
        # dropout too brief for the coverage check to have caught.
        detail = (
            f"the file's own total exceeds the GPS distance by {(factor - 1) * 100:.1f}%, "
            f"which GPS noise cannot explain; part of the route was not recorded"
        )
        for activity in scalable:
            _skip(skips, warnings, tcx.label(activity), SkipReason.PARTIAL_GPS, detail)
        warnings.append("no activity carries a usable GPS distance stream; returned unchanged")
        return _unchanged(tcx_bytes, target_distance_m, trackpoint_count, warnings, skips)

    # The guard is asymmetric because the two directions are different failures.
    # Below 1 the stream over-measured: ordinary GPS jitter, observed as far down
    # as 0.723 on a real walk, so the bound is loose. Above 1 the stream
    # under-measured, which jitter cannot cause — a target read from the file is
    # handled as partial GPS above, and an explicit target that high is a caller
    # error, so it is still bounded.
    lower = 1.0 - tolerance
    upper = 1.0 + tolerance
    too_low = factor < lower
    too_high = not from_file and factor > upper
    if too_low or too_high:
        if on_tolerance is ToleranceAction.ABORT:
            raise ToleranceExceeded(factor, gps_total, target_distance_m, tolerance)
        if on_tolerance is ToleranceAction.CLAMP:
            clamped = lower if too_low else upper
            warnings.append(f"factor {factor:.4f} outside tolerance, clamped to {clamped:.4f}")
            factor = clamped
        else:
            warnings.append(f"factor {factor:.4f} outside tolerance {tolerance:.4f}, proceeding")

    for activity in scalable:
        _scale(activity, factor, _lap_factor(activity, target_distance_m, lap_totals))

    return RescaleResult(
        data=tcx.serialise(root),
        gps_total_m=gps_total,
        target_m=target_distance_m,
        factor=factor,
        trackpoint_count=trackpoint_count,
        warnings=tuple(warnings),
        modified=True,
        skips=tuple(skips),
    )


def _target_from_file(scalable: list[ET.Element], lap_totals: dict[int, float | None]) -> float:
    """Fitbit's own total for the activities being scaled, or raise `MissingTarget`."""
    total = 0.0
    found = False
    for activity in scalable:
        lap_total = lap_totals[id(activity)]
        if lap_total is not None:
            total += lap_total
            found = True
    if not found or total <= 0:
        raise MissingTarget(
            "no target distance given and the file carries no usable "
            "Lap/DistanceMeters to take one from; pass an explicit distance"
        )
    return total


def _distance_stream(activity: ET.Element) -> tuple[float | None, bool, int]:
    """Final cumulative distance, whether it ever went backwards, trackpoint count."""
    final: float | None = None
    previous: float | None = None
    non_monotonic = False
    count = 0
    for point in tcx.trackpoints(activity):
        count += 1
        element = point.find(tcx.DISTANCE_METERS)
        if element is None:
            continue
        value = tcx.read_float(element)
        if previous is not None and value < previous:
            non_monotonic = True
        previous = value
        final = value
    return final, non_monotonic, count


def _lap_factor(
    activity: ET.Element, target_m: float, lap_totals: dict[int, float | None]
) -> float:
    """What to multiply `Lap/DistanceMeters` by so the laps sum to the target.

    Exactly 1.0 whenever the target came from the file, which is the common case:
    Fitbit's own total is already correct and must survive untouched.
    """
    lap_total = lap_totals[id(activity)]
    if not lap_total:
        return 1.0
    share = target_m * (lap_total / sum(t for t in lap_totals.values() if t))
    return share / lap_total


def _scale(activity: ET.Element, factor: float, lap_factor: float) -> None:
    # Lap totals are Fitbit's figure, not GPS, so they take their own factor —
    # 1.0 when the target came from them.
    for lap in activity.iter(tcx.LAP):
        element = lap.find(tcx.DISTANCE_METERS)
        if element is not None:
            element.text = _format(tcx.read_float(element) * lap_factor)
    for point in tcx.trackpoints(activity):
        element = point.find(tcx.DISTANCE_METERS)
        if element is not None:
            element.text = _format(tcx.read_float(element) * factor)
    for element in activity.iter():
        if element.tag in _SCALED_SPEED_TAGS:
            element.text = _format(tcx.read_float(element) * factor)


def _format(value: float) -> str:
    """Write a float without trailing noise: 10201.0 -> '10201', not '10201.000000001'."""
    return f"{value:.{_PRECISION}f}".rstrip("0").rstrip(".")


def _skip(
    skips: list[Skip], warnings: list[str], name: str, reason: SkipReason, detail: str
) -> None:
    skips.append(Skip(activity=name, reason=reason, detail=detail))
    warnings.append(f"activity {name}: {detail}, left unchanged")


def _unchanged(
    tcx_bytes: bytes,
    target_m: float | None,
    trackpoint_count: int,
    warnings: list[str],
    skips: list[Skip],
) -> RescaleResult:
    """Hand back the original bytes untouched, not a re-serialisation of them."""
    return RescaleResult(
        data=tcx_bytes,
        gps_total_m=0.0,
        target_m=target_m,
        factor=1.0,
        trackpoint_count=trackpoint_count,
        warnings=tuple(warnings),
        modified=False,
        skips=tuple(skips),
    )
