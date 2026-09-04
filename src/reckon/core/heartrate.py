"""Merging a heart-rate series back into a TCX.

Google Health's `:exportExerciseTcx` exports the *route*. It carries position,
altitude and distance, and no heart rate at all — the same walk exported by hand
from the app has it on 193 of its trackpoints. So an activity Reckon uploads
loses a trace the device recorded, unless it is put back.

Pure, like everything else in `core`: samples in, bytes out, no clock and no
network. The client fetches the series; this decides what to do with it.

Two rules the merge must not break, both tested:

- **Timestamps are never modified.** This writes a new child into a trackpoint
  and touches nothing else. The same invariant `rescale` holds.
- **Existing data is never overwritten.** A trackpoint that already carries heart
  rate keeps what it has. That makes the merge idempotent, and it means running
  it over an app export — which does have heart rate — changes nothing.
"""

import datetime as dt
import xml.etree.ElementTree as ET
from bisect import bisect_left
from dataclasses import dataclass

from reckon.core import tcx

HEART_RATE_BPM = tcx.qn(tcx.TCX_NS, "HeartRateBpm")
VALUE = tcx.qn(tcx.TCX_NS, "Value")

# How far a sample may sit from a trackpoint and still be used for it.
#
# A wrist sensor samples every few seconds and on its own clock, so exact matches
# are the exception. Ten seconds is wide enough to bridge that and narrow enough
# that a gap in the series shows up as missing heart rate rather than as a stale
# reading smeared across it.
DEFAULT_TOLERANCE_S = 10.0

# TCX is a sequenced schema: a Trackpoint's children must appear in this order,
# and a file that puts HeartRateBpm in the wrong place is invalid even though it
# parses. Strava has not been observed to reject one, but writing invalid XML on
# the assumption that nobody checks is how a silent failure gets built.
_TRACKPOINT_ORDER = [
    tcx.qn(tcx.TCX_NS, "Time"),
    tcx.qn(tcx.TCX_NS, "Position"),
    tcx.qn(tcx.TCX_NS, "AltitudeMeters"),
    tcx.qn(tcx.TCX_NS, "DistanceMeters"),
    HEART_RATE_BPM,
    tcx.qn(tcx.TCX_NS, "Cadence"),
    tcx.qn(tcx.TCX_NS, "SensorState"),
    tcx.qn(tcx.TCX_NS, "Extensions"),
]


@dataclass(frozen=True)
class MergeResult:
    """The document with heart rate in it, and how well it went."""

    data: bytes
    trackpoints: int
    matched: int
    already_present: int
    samples: int

    @property
    def coverage(self) -> float:
        """Share of trackpoints that ended up carrying heart rate."""
        if self.trackpoints == 0:
            return 0.0
        return (self.matched + self.already_present) / self.trackpoints


def merge(
    tcx_bytes: bytes,
    samples: list[tuple[dt.datetime, int]],
    *,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> MergeResult:
    """Write `samples` into the document's trackpoints by nearest timestamp."""
    root = tcx.parse(tcx_bytes)
    ordered = sorted(samples, key=lambda pair: pair[0])
    times = [moment for moment, _ in ordered]

    trackpoints = matched = already = 0
    for activity in tcx.activities(root):
        for point in tcx.trackpoints(activity):
            trackpoints += 1
            if point.find(HEART_RATE_BPM) is not None:
                already += 1
                continue
            element = point.find(tcx.TIME)
            if element is None:
                continue
            bpm = _nearest(times, ordered, tcx.read_time(element), tolerance_s)
            if bpm is None:
                continue
            _insert(point, bpm)
            matched += 1

    return MergeResult(
        data=tcx.serialise(root),
        trackpoints=trackpoints,
        matched=matched,
        already_present=already,
        samples=len(ordered),
    )


def _nearest(
    times: list[dt.datetime],
    samples: list[tuple[dt.datetime, int]],
    moment: dt.datetime,
    tolerance_s: float,
) -> int | None:
    """The closest sample to `moment`, or None if the nearest is too far away."""
    if not samples:
        return None
    index = bisect_left(times, moment)
    best: tuple[float, int] | None = None
    for candidate in (index - 1, index):
        if 0 <= candidate < len(samples):
            gap = abs((samples[candidate][0] - moment).total_seconds())
            if best is None or gap < best[0]:
                best = (gap, samples[candidate][1])
    if best is None or best[0] > tolerance_s:
        return None
    return best[1]


def _insert(point: ET.Element, bpm: int) -> None:
    """Add `<HeartRateBpm><Value>bpm</Value></HeartRateBpm>` in schema order."""
    element = ET.Element(HEART_RATE_BPM)
    value = ET.SubElement(element, VALUE)
    value.text = str(bpm)

    position = _TRACKPOINT_ORDER.index(HEART_RATE_BPM)
    for index, existing in enumerate(point):
        if existing.tag in _TRACKPOINT_ORDER and _TRACKPOINT_ORDER.index(existing.tag) > position:
            point.insert(index, element)
            return
    point.append(element)
