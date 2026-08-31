"""Parse and serialise Garmin TCX documents.

Owns namespaces, element lookup and the round-trip guarantees the transform
depends on. Knows nothing about rescaling.
"""

import datetime as dt
import math
import statistics
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass

from reckon.core.errors import MalformedTCX

TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
AX_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"


def qn(namespace: str, tag: str) -> str:
    """Qualified ElementTree tag name."""
    return f"{{{namespace}}}{tag}"


ROOT = qn(TCX_NS, "TrainingCenterDatabase")
ACTIVITY = qn(TCX_NS, "Activity")
LAP = qn(TCX_NS, "Lap")
TRACKPOINT = qn(TCX_NS, "Trackpoint")
POSITION = qn(TCX_NS, "Position")
DISTANCE_METERS = qn(TCX_NS, "DistanceMeters")
TIME = qn(TCX_NS, "Time")
ACTIVITY_ID = qn(TCX_NS, "Id")

# ElementTree emits ns0:/ns1: prefixes on output unless the namespaces are
# registered, which would leave the file technically equivalent but textually
# unlike anything Fitbit or Garmin produce. Registering TCX as the *default*
# namespace is what keeps output looking like input. This is global state inside
# ElementTree; it is set once, at import, and never varies.
#
# Garmin writes the ActivityExtension namespace as `ns2:`, but ElementTree
# reserves the `ns<digits>` prefix form for its own use and raises on any attempt
# to register one. `ax` is used instead: equivalent XML, different prefix. Real
# Fitbit exports carry no Extensions elements at all, so this prefix reaches the
# output only for synthetic fixtures.
ET.register_namespace("", TCX_NS)
ET.register_namespace("ax", AX_NS)


def parse(data: bytes) -> ET.Element:
    """Parse TCX bytes into a root element, or raise `MalformedTCX`."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MalformedTCX(f"not well-formed XML: {exc}") from exc
    if root.tag != ROOT:
        raise MalformedTCX(f"root element is {root.tag!r}, expected a TCX TrainingCenterDatabase")
    return root


def serialise(root: ET.Element) -> bytes:
    """Serialise back to bytes, preserving the default namespace."""
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def activities(root: ET.Element) -> Iterator[ET.Element]:
    yield from root.iter(ACTIVITY)


def trackpoints(element: ET.Element) -> Iterator[ET.Element]:
    yield from element.iter(TRACKPOINT)


def has_position(activity: ET.Element) -> bool:
    """True when the activity carries GPS coordinates at all."""
    return next(activity.iter(POSITION), None) is not None


def label(activity: ET.Element) -> str:
    """Human-readable identifier for an activity, for warning messages."""
    element = activity.find(ACTIVITY_ID)
    if element is None or not (element.text or "").strip():
        return "<unidentified>"
    return element.text.strip()


def gps_coverage(activity: ET.Element) -> float:
    """Fraction of the activity's elapsed time that carries a position fix.

    Measured in *seconds*, not trackpoints, and deliberately so: the watch
    samples roughly half as often while it has no fix, so counting trackpoints
    understates a dropout badly enough to hide one.

    An interval between two trackpoints counts as covered when the earlier of the
    two has a `Position`. Returns 0.0 for an activity with no positions at all,
    and 1.0 for one too short to have an interval.
    """
    marks: list[tuple[dt.datetime, bool]] = []
    for point in trackpoints(activity):
        element = point.find(TIME)
        if element is None:
            raise MalformedTCX("trackpoint has no Time; cannot measure GPS coverage")
        marks.append((read_time(element), point.find(POSITION) is not None))
    if not marks:
        return 0.0
    span = (marks[-1][0] - marks[0][0]).total_seconds()
    if span <= 0:
        return 1.0 if any(covered for _, covered in marks) else 0.0
    unlocked = sum(
        (marks[i][0] - marks[i - 1][0]).total_seconds()
        for i in range(1, len(marks))
        if not marks[i - 1][1]
    )
    return max(0.0, 1.0 - unlocked / span)


# What counts as a break in the recording rather than normal sampling, expressed
# as a multiple of the file's *own* median interval with an absolute floor.
#
# Relative rather than fixed, because "normal" differs per file: the corpus holds
# files sampling at 1 s and at 2 s, and a synthetic fixture may sample at 10 s.
# An absolute threshold would call ordinary sampling a gap on any of them.
#
# The multiple is measured, not chosen. Seventeen of the twenty corpus files have
# a maximum interval of exactly 3 s against a 1 s median — three times — and none
# exceeds it. The 2 s-median files also top out at 3 s, comfortably inside their
# own 6 s threshold. Both thresholds reproduce the corpus exactly, and the floor
# stops a hypothetical sub-second recorder from calling every sample a gap.
GAP_MULTIPLE = 3.0
GAP_FLOOR_SECONDS = 3.0


@dataclass(frozen=True)
class Gaps:
    """Time the recorder skipped entirely, as distinct from time without a fix.

    `gps_coverage` answers "did the trackpoints have positions". This answers
    "were there trackpoints at all". They are different failures and only one of
    them was visible before: a file whose every trackpoint carries a fix reports
    100% coverage even when half its elapsed time falls between trackpoints.

    Distance is *not* lost to a gap — the stream chords straight across it — so
    this is not grounds for refusing to correct. Shape is lost, which is worth
    saying out loud.
    """

    count: int
    total_s: float
    largest_s: float
    span_s: float

    @property
    def fraction(self) -> float:
        """Share of elapsed time with no trackpoint in it."""
        return 0.0 if self.span_s <= 0 else self.total_s / self.span_s


def recording_gaps(activity: ET.Element, minimum: float | None = None) -> Gaps:
    """Intervals between consecutive trackpoints that are breaks, not sampling.

    `minimum` defaults to `max(GAP_FLOOR_SECONDS, GAP_MULTIPLE x median interval)`,
    so the answer does not depend on how often this particular recorder writes.
    """
    times: list[dt.datetime] = []
    for point in trackpoints(activity):
        element = point.find(TIME)
        if element is None:
            raise MalformedTCX("trackpoint has no Time; cannot measure recording gaps")
        times.append(read_time(element))
    if len(times) < 2:
        return Gaps(0, 0.0, 0.0, 0.0)

    spans = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
    if minimum is None:
        minimum = max(GAP_FLOOR_SECONDS, GAP_MULTIPLE * statistics.median(spans))
    over = [s for s in spans if s > minimum]
    return Gaps(
        count=len(over),
        total_s=sum(over),
        largest_s=max(spans),
        span_s=(times[-1] - times[0]).total_seconds(),
    )


def read_time(element: ET.Element) -> dt.datetime:
    """Parse a TCX timestamp for measurement only — it is never written back."""
    text = (element.text or "").strip()
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise MalformedTCX(f"Time is not an ISO 8601 timestamp: {text!r}") from exc


def lap_distance_total(activity: ET.Element) -> float | None:
    """Sum of the activity's `Lap/DistanceMeters`, or None when it carries none.

    This is Fitbit's own total for the activity — the stride-fused figure the app
    displays — as distinct from the cumulative GPS stream on the trackpoints.
    Confirmed against Google Health on every file in the calibration corpus, so
    it is the default target the transform rescales *to*.
    """
    total: float | None = None
    for lap in activity.iter(LAP):
        element = lap.find(DISTANCE_METERS)
        if element is None:
            continue
        total = (total or 0.0) + read_float(element)
    return total


def read_float(element: ET.Element) -> float:
    """Read an element's text as a finite float, or raise `MalformedTCX`."""
    text = (element.text or "").strip()
    try:
        value = float(text)
    except ValueError as exc:
        raise MalformedTCX(f"{_local(element.tag)} is not a number: {text!r}") from exc
    if not math.isfinite(value):
        raise MalformedTCX(f"{_local(element.tag)} is not finite: {text!r}")
    return value


def timestamps(root: ET.Element) -> list[str | None]:
    """Every time-bearing value in the document, in document order.

    The transform must never touch any of these. Comparing this list before and
    after is how `test_rescale.py` holds the temporal invariant.
    """
    found: list[str | None] = []
    for element in root.iter():
        if element.tag in (ACTIVITY_ID, TIME):
            found.append(element.text)
        elif element.tag == LAP:
            found.append(element.get("StartTime"))
    return found


def _local(tag: str) -> str:
    """Strip the namespace from a qualified tag, for readable messages."""
    return tag.rpartition("}")[2]
