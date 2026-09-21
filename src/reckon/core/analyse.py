"""Corpus statistics: what a body of real exports says about the transform.

Pure, like the rest of `core` — every function here takes bytes and returns
numbers. Walking a directory is the CLI's job.

The measurements are chosen to answer questions the corpus is the only place to
settle: how far the factor varies, how much of an activity the GPS actually
covered, how noisy the track was, and how much the derived figures move when the
distance stream is rescaled.
"""

import datetime as dt
import math
import statistics as st
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from reckon.core import tcx
from reckon.core.errors import MissingTarget
from reckon.core.rescale import (
    DEFAULT_TOLERANCE,
    MAX_CREDIBLE_FACTOR,
    MAX_GAP_FRACTION,
    MAX_UNRECORDED_FRACTION,
    MIN_GPS_COVERAGE,
    RescaleResult,
    SkipReason,
    rescale_tcx,
)

# Speed below which a sample counts as stopped, when estimating moving time.
# Only ever used to compare a before against an after, so the absolute value
# matters far less than its being fixed.
MOVING_SPEED_MS = 0.5

# Interval used to decimate a track when measuring wiggle. Real movement is
# smooth at this scale and GPS jitter is not, so the ratio of full-resolution
# path length to decimated path length isolates high-frequency noise from
# genuine cornering.
WIGGLE_INTERVAL_S = 5

_EARTH_RADIUS_M = 6371008.8


@dataclass(frozen=True)
class ActivityStats:
    """Everything the corpus reports about one activity."""

    sport: str
    trackpoints: int
    trackpoints_with_gps: int
    elapsed_s: float
    lap_total_time_s: float
    lap_distance_m: float | None
    stream_distance_m: float | None
    haversine_m: float
    factor: float | None
    gps_coverage: float
    gap_fraction: float
    unrecorded_fraction: float
    median_interval_s: float
    max_interval_s: float
    wiggle: float | None
    lead_in_s: float | None
    start_lag_s: float | None
    moving_before_s: float
    moving_after_s: float
    skipped: tuple[SkipReason, ...]
    no_target: bool = False
    """The file has a GPS stream to correct but carries no total to correct it to."""

    @property
    def corrected(self) -> bool:
        """Whether the transform actually changed this file."""
        return self.factor is not None

    @property
    def inflation(self) -> float | None:
        """How much longer the GPS stream is than the activity's own total."""
        if self.factor is None or self.factor <= 0:
            return None
        return 1.0 / self.factor - 1.0

    @property
    def moving_delta_s(self) -> float:
        """How far the moving-time estimate moves when the stream is rescaled."""
        return self.moving_after_s - self.moving_before_s


@dataclass(frozen=True)
class CorpusSummary:
    """Aggregate over the activities that were actually corrected."""

    files: int
    corrected: int
    factor_mean: float | None
    factor_stdev: float | None
    factor_min: float | None
    factor_max: float | None
    worst_moving_delta_s: float
    skipped: tuple[tuple[SkipReason, int], ...]


@dataclass(frozen=True)
class ThresholdEvidence:
    """What the corpus currently says about one threshold constant.

    Exists because every corpus-citing comment in this codebase has eventually
    been falsified by a file, twice needing rewriting by hand. The shape is the
    same for all six constants: which files the measure applies to, the ones
    nearest the bound on either side, and the clearance between them. That last
    number is the one that keeps eroding, so it is computed rather than claimed.

    `below` and `above` are sorted *towards* the bound, nearest first, so the
    first entry of each is the pair the clearance is measured across. Which side
    is the safe one differs per constant and is deliberately not encoded here:
    this reports, it does not judge.
    """

    name: str
    value: float
    measure: str
    """What was measured, worded for a reader comparing it against a comment."""

    below: tuple[tuple[str, float], ...]
    above: tuple[tuple[str, float], ...]

    @property
    def population(self) -> int:
        """Files the measure applies to. Not every constant sees every file."""
        return len(self.below) + len(self.above)

    @property
    def clearance(self) -> float | None:
        """Distance between the nearest file each side. None when one side is empty.

        An empty side is the honest answer, not a wide margin: it means the
        corpus has nothing there yet, which is exactly the claim that has twice
        stopped being true.
        """
        if not self.below or not self.above:
            return None
        return self.above[0][1] - self.below[0][1]


def threshold_evidence(measured: list[tuple[str, ActivityStats]]) -> list[ThresholdEvidence]:
    """Recompute the corpus figures behind each threshold, for checking comments against.

    Reports; never judges. A constant is not wrong because a file crossed it —
    `MAX_GAP_FRACTION` is *meant* to have files above it — so the caller reads
    the numbers and decides, exactly as the comments record having done.
    """
    # Each constant sees a different population, and the differences are the
    # point: a no-GPS yoga file says nothing about GPS coverage, and a skipped
    # file has no factor to be inside a tolerance. Folding them in would put
    # zeroes next to the bound and invent a clearance that is not there.
    gps = [(n, s.gps_coverage) for n, s in measured if s.trackpoints_with_gps > 0]
    # The *implied* factor, not the one the transform ran with, and the
    # difference is the whole point of this row. MAX_CREDIBLE_FACTOR decides
    # whether a candidate factor is believable, so a file it refuses is exactly
    # the evidence it is about — and a refused file has no `factor` at all.
    # Reading `s.factor` here made the row circular: it could only ever report
    # the files that passed, and answered "0 above" on a corpus containing one
    # above. Found 2026-09-21, two days after this was written.
    complete = [
        (n, s.lap_distance_m / s.stream_distance_m)
        for n, s in measured
        if s.gps_coverage >= 1.0 and s.lap_distance_m is not None and s.stream_distance_m
    ]
    corrected = [(n, abs(s.factor - 1.0)) for n, s in measured if s.factor is not None]
    sampled = [
        (n, s.max_interval_s / s.median_interval_s) for n, s in measured if s.median_interval_s > 0
    ]

    return [
        _split(
            "MIN_GPS_COVERAGE",
            MIN_GPS_COVERAGE,
            "GPS coverage, files carrying any",
            gps,
        ),
        _split(
            "MAX_CREDIBLE_FACTOR",
            MAX_CREDIBLE_FACTOR,
            "implied factor, complete tracks only",
            complete,
        ),
        _split(
            "DEFAULT_TOLERANCE",
            DEFAULT_TOLERANCE,
            "distance of factor from 1",
            corrected,
        ),
        _split(
            "MAX_GAP_FRACTION",
            MAX_GAP_FRACTION,
            "elapsed time between trackpoints",
            [(n, s.gap_fraction) for n, s in measured],
        ),
        _split(
            "MAX_UNRECORDED_FRACTION",
            MAX_UNRECORDED_FRACTION,
            "activity duration outside the track",
            [(n, s.unrecorded_fraction) for n, s in measured],
        ),
        _split(
            "GAP_MULTIPLE",
            tcx.GAP_MULTIPLE,
            "longest interval over the file's own median",
            sampled,
        ),
    ]


def _split(
    name: str, value: float, measure: str, rows: list[tuple[str, float]]
) -> ThresholdEvidence:
    """Partition measurements either side of the bound, each sorted towards it."""
    return ThresholdEvidence(
        name=name,
        value=value,
        measure=measure,
        below=tuple(sorted((r for r in rows if r[1] <= value), key=lambda r: -r[1])),
        above=tuple(sorted((r for r in rows if r[1] > value), key=lambda r: r[1])),
    )


def analyse_tcx(data: bytes) -> ActivityStats:
    """Measure one TCX file, running the transform to find its factor."""
    root = tcx.parse(data)
    activity = next(tcx.activities(root), None)
    if activity is None:
        raise tcx.MalformedTCX("no Activity element to analyse")

    samples = _samples(activity)
    result = _rescale_quietly(data)
    factor = result.factor if result is not None and result.modified else None

    positions = [(t, p) for t, p, _ in samples if p is not None]
    stream = [(t, d) for t, d in ((t, d) for t, _, d in samples) if d is not None]
    elapsed = (samples[-1][0] - samples[0][0]).total_seconds() if len(samples) > 1 else 0.0
    intervals = [
        (samples[i][0] - samples[i - 1][0]).total_seconds() for i in range(1, len(samples))
    ]

    return ActivityStats(
        sport=activity.get("Sport") or "",
        trackpoints=len(samples),
        trackpoints_with_gps=len(positions),
        elapsed_s=elapsed,
        lap_total_time_s=_lap_time(activity),
        lap_distance_m=tcx.lap_distance_total(activity),
        stream_distance_m=stream[-1][1] if stream else None,
        haversine_m=_path([p for _, p in positions]),
        factor=factor,
        gps_coverage=tcx.gps_coverage(activity),
        gap_fraction=tcx.recording_gaps(activity).fraction,
        unrecorded_fraction=tcx.unrecorded_time(activity).fraction,
        median_interval_s=st.median(intervals) if intervals else 0.0,
        max_interval_s=max(intervals, default=0.0),
        wiggle=_wiggle(positions),
        lead_in_s=(positions[0][0] - samples[0][0]).total_seconds() if positions else None,
        start_lag_s=_start_lag(activity, samples[0][0]),
        moving_before_s=_moving_time(stream, 1.0),
        moving_after_s=_moving_time(stream, factor if factor is not None else 1.0),
        skipped=() if result is None else tuple(s.reason for s in result.skips),
        no_target=result is None,
    )


def summarise(stats: list[ActivityStats]) -> CorpusSummary:
    """Aggregate per-file measurements into the numbers worth quoting."""
    factors = [s.factor for s in stats if s.factor is not None]
    counts: dict[SkipReason, int] = {}
    for s in stats:
        for reason in set(s.skipped):
            counts[reason] = counts.get(reason, 0) + 1
    return CorpusSummary(
        files=len(stats),
        corrected=len(factors),
        factor_mean=st.mean(factors) if factors else None,
        factor_stdev=st.stdev(factors) if len(factors) > 1 else None,
        factor_min=min(factors) if factors else None,
        factor_max=max(factors) if factors else None,
        worst_moving_delta_s=max((abs(s.moving_delta_s) for s in stats), default=0.0),
        skipped=tuple(sorted(counts.items())),
    )


def _rescale_quietly(data: bytes) -> RescaleResult | None:
    """Rescale with the guards wide open, so analysis measures rather than judges.

    None when the file has a stream to correct but no total to correct it to —
    a fact worth reporting about a corpus, not a reason to abandon the file.
    """
    try:
        return rescale_tcx(data, tolerance=1.0)
    except MissingTarget:
        return None


def _samples(
    activity: ET.Element,
) -> list[tuple[dt.datetime, tuple[float, float] | None, float | None]]:
    rows = []
    for point in tcx.trackpoints(activity):
        element = point.find(tcx.TIME)
        if element is None:
            raise tcx.MalformedTCX("trackpoint has no Time; cannot analyse")
        position = point.find(tcx.POSITION)
        distance = point.find(tcx.DISTANCE_METERS)
        rows.append(
            (
                tcx.read_time(element),
                None if position is None else _coordinates(position),
                None if distance is None else tcx.read_float(distance),
            )
        )
    if not rows:
        raise tcx.MalformedTCX("activity has no trackpoints to analyse")
    return rows


def _coordinates(position: ET.Element) -> tuple[float, float]:
    latitude = position.find(tcx.qn(tcx.TCX_NS, "LatitudeDegrees"))
    longitude = position.find(tcx.qn(tcx.TCX_NS, "LongitudeDegrees"))
    if latitude is None or longitude is None:
        raise tcx.MalformedTCX("Position is missing a latitude or longitude")
    return tcx.read_float(latitude), tcx.read_float(longitude)


def _lap_time(activity: ET.Element) -> float:
    total = 0.0
    for lap in activity.iter(tcx.LAP):
        element = lap.find(tcx.qn(tcx.TCX_NS, "TotalTimeSeconds"))
        if element is not None:
            total += tcx.read_float(element)
    return total


def _start_lag(activity: ET.Element, first_sample: dt.datetime) -> float | None:
    """How far the first trackpoint post-dates the activity's own start time."""
    element = activity.find(tcx.ACTIVITY_ID)
    if element is None or not (element.text or "").strip():
        return None
    try:
        start = tcx.read_time(element)
    except tcx.MalformedTCX:
        # Some producers put a non-timestamp identifier in Id; that is not an
        # error here, there is simply no lag to report.
        return None
    return (first_sample - start).total_seconds()


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat = lat2 - lat1
    dlon = math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _path(points: list[tuple[float, float]]) -> float:
    return sum(_haversine(points[i - 1], points[i]) for i in range(1, len(points)))


def _wiggle(positions: list[tuple[dt.datetime, tuple[float, float]]]) -> float | None:
    """Full-resolution path length over the same path sampled every few seconds."""
    if len(positions) < 2:
        return None
    start = positions[0][0]
    decimated = [
        p for t, p in positions if int((t - start).total_seconds()) % WIGGLE_INTERVAL_S == 0
    ]
    coarse = _path(decimated)
    if coarse <= 0:
        return None
    return _path([p for _, p in positions]) / coarse


def _moving_time(stream: list[tuple[dt.datetime, float]], factor: float) -> float:
    """Seconds spent above the moving threshold, with distances scaled by `factor`."""
    total = 0.0
    for i in range(1, len(stream)):
        seconds = (stream[i][0] - stream[i - 1][0]).total_seconds()
        if seconds <= 0:
            continue
        advance = (stream[i][1] - stream[i - 1][1]) * factor
        if advance / seconds >= MOVING_SPEED_MS:
            total += seconds
    return total
