"""Parse and serialise Garmin TCX documents.

Owns namespaces, element lookup and the round-trip guarantees the transform
depends on. Knows nothing about rescaling.
"""

import math
import xml.etree.ElementTree as ET
from collections.abc import Iterator

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
