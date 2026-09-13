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
AVERAGE_HEART_RATE_BPM = tcx.qn(tcx.TCX_NS, "AverageHeartRateBpm")
VALUE = tcx.qn(tcx.TCX_NS, "Value")
TRACK = tcx.qn(tcx.TCX_NS, "Track")

# A Lap's children, in the order the schema requires.
_LAP_ORDER = [
    tcx.qn(tcx.TCX_NS, "TotalTimeSeconds"),
    tcx.qn(tcx.TCX_NS, "DistanceMeters"),
    tcx.qn(tcx.TCX_NS, "MaximumSpeed"),
    tcx.qn(tcx.TCX_NS, "Calories"),
    AVERAGE_HEART_RATE_BPM,
    tcx.qn(tcx.TCX_NS, "MaximumHeartRateBpm"),
    tcx.qn(tcx.TCX_NS, "Intensity"),
    tcx.qn(tcx.TCX_NS, "Cadence"),
    tcx.qn(tcx.TCX_NS, "TriggerMethod"),
    tcx.qn(tcx.TCX_NS, "Track"),
]

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


@dataclass(frozen=True)
class BuildResult:
    """The document with a heart-rate track in it, and what was made."""

    data: bytes
    created: int
    annotated: int
    outside: int
    skipped_activities: int


def build(
    tcx_bytes: bytes,
    samples: list[tuple[dt.datetime, int]],
    *,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> BuildResult:
    """Give a GPS-less activity the per-trackpoint heart rate the API omits.

    `:exportExerciseTcx` returns a **two-trackpoint skeleton** for an activity
    with no GPS — verified against the live API on 2026-09-12, where a yoga
    session and a walk-plus-train came back with two bare `<Time>` trackpoints
    and no heart rate, against 373 in the phone app's export of the same walk.
    Merging a series onto two trackpoints gives two readings, and Relative Effort
    needs time in zones, so deployed mode would contribute nothing to Fitness for
    exactly the activities local mode proved gain the most.

    **This is not the fabrication the project forbids.** That rule says: never
    synthesise a per-trackpoint trace from an *average*, because a flat line
    across a run looks like data and is not. Here every value is a real
    per-second reading from the `heart-rate` data type, and the output is the
    shape the phone app itself writes — the corpus's yoga file is 1867
    trackpoints of `Time` and `HeartRateBpm` with no position and no distance,
    and Strava computed Relative Effort from it. Assembling measurements into a
    format is not deriving many values from one. See `ARCHITECTURE.md`.

    Three rules, each of which would be a bug to drop:

    - **Only activities with no GPS.** Where there is a track, `merge` annotates
      what is already there and nothing is created.
    - **The existing trackpoints survive.** They mark the activity's start and
      end, and Strava reads elapsed time from the trackpoint span rather than
      from `TotalTimeSeconds` — dropping them would shorten the activity by
      however long the watch waited for its first reading.
    - **Nothing outside the lap's own span.** A sample from before the activity
      began or after it ended is dropped rather than clamped: clamping would
      invent a reading at a time it was not taken, which is the line this
      function is careful to stay on the right side of.
    """
    root = tcx.parse(tcx_bytes)
    ordered = sorted(samples, key=lambda pair: pair[0])
    times = [moment for moment, _ in ordered]

    created = annotated = outside = skipped = 0
    for activity in tcx.activities(root):
        if tcx.has_position(activity):
            skipped += 1
            continue
        for lap in activity.iter(tcx.LAP):
            track = lap.find(TRACK)
            if track is None:
                continue
            points = list(track)
            entries = [
                (tcx.read_time(element), point)
                for point in points
                for element in [point.find(tcx.TIME)]
                if element is not None
            ]
            # Every child must be a trackpoint carrying a time, or this lap is
            # left alone. The track is rewritten in time order below, and
            # reordering a track holding something unrecognised would move it
            # somewhere arbitrary. `merge` still annotates such a lap; only the
            # creating half declines.
            if not entries or len(entries) != len(points):
                continue
            first, last = entries[0][0], entries[-1][0]

            for moment, point in entries:
                if point.find(HEART_RATE_BPM) is not None:
                    continue
                bpm = _nearest(times, ordered, moment, tolerance_s)
                if bpm is not None:
                    _insert(point, bpm)
                    annotated += 1

            # Measured against the trackpoints that were *already here*, never
            # against the ones being created. Checking the growing list made
            # `tolerance_s` do a second job it was never meant for — a minimum
            # spacing between new points — so one created at t suppressed every
            # sample up to t+10 and a 1 Hz series came out at one point per
            # 11 s, discarding 91% of the readings this function exists to
            # preserve. One mechanism, two meanings, again.
            existing = [moment for moment, _ in entries]
            written: set[dt.datetime] = set()
            for moment, bpm in ordered:
                if not first <= moment <= last:
                    outside += 1
                    continue
                if moment in written:
                    continue
                if any(abs((moment - held).total_seconds()) <= tolerance_s for held in existing):
                    continue
                entries.append((moment, _trackpoint(moment, bpm)))
                written.add(moment)
                created += 1

            # Rewritten from the pairs rather than sorted in place, so ordering
            # never re-reads a `Time` that was already parsed.
            entries.sort(key=lambda pair: pair[0])
            for point in list(track):
                track.remove(point)
            track.extend(point for _, point in entries)

    return BuildResult(
        data=tcx.serialise(root),
        created=created,
        annotated=annotated,
        outside=outside,
        skipped_activities=skipped,
    )


def _trackpoint(moment: dt.datetime, bpm: int) -> ET.Element:
    """One `<Trackpoint>` carrying a time and a reading, and nothing else.

    No `Position` and no `DistanceMeters`, deliberately: there is no GPS here and
    inventing either would be the fabrication this module exists to avoid. The
    shape matches what the phone app writes for the same activity.
    """
    point = ET.Element(tcx.TRACKPOINT)
    ET.SubElement(point, tcx.TIME).text = moment.isoformat().replace("+00:00", "Z")
    _insert(point, bpm)
    return point


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

    _insert_ordered(point, element, _TRACKPOINT_ORDER)


def set_average(tcx_bytes: bytes, bpm: int) -> tuple[bytes, str | None]:
    """Write an activity's average heart rate onto its lap.

    The fallback for when the per-trackpoint series is out of reach: the exercise
    summary carries an average that the activity scope alone can read, where the
    per-second series needs a restricted scope and Google's verification process
    behind it. It gives a number rather than a graph.

    **Only when the activity has exactly one lap.** An activity average is a
    property of the activity, and writing it onto each of several laps would
    assert something about each lap that was never measured. All observed exports
    are single-lap, so this is a guard rather than a limitation, and it returns
    the reason when it declines.

    An existing value is never replaced.
    """
    root = tcx.parse(tcx_bytes)
    written = 0
    for activity in tcx.activities(root):
        laps = list(activity.iter(tcx.LAP))
        if len(laps) != 1:
            return tcx_bytes, (
                f"average heart rate not written: {tcx.label(activity)} has "
                f"{len(laps)} laps, and an activity average is not a lap average"
            )
        lap = laps[0]
        if lap.find(AVERAGE_HEART_RATE_BPM) is not None:
            continue
        element = ET.Element(AVERAGE_HEART_RATE_BPM)
        ET.SubElement(element, VALUE).text = str(bpm)
        _insert_ordered(lap, element, _LAP_ORDER)
        written += 1
    if written == 0:
        return tcx_bytes, None
    return tcx.serialise(root), None


def _insert_ordered(parent: ET.Element, element: ET.Element, order: list[str]) -> None:
    """Place `element` among `parent`'s children according to `order`."""
    position = order.index(element.tag)
    for index, existing in enumerate(parent):
        if existing.tag in order and order.index(existing.tag) > position:
            parent.insert(index, element)
            return
    parent.append(element)
