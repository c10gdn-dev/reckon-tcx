"""Parsing, serialisation and namespace handling."""

import xml.etree.ElementTree as ET

import pytest

import builders
from reckon.core import tcx
from reckon.core.errors import MalformedTCX


def element(text: str | None, tag: str = "DistanceMeters") -> ET.Element:
    node = ET.Element(tcx.qn(tcx.TCX_NS, tag))
    node.text = text
    return node


def test_parse_returns_the_root_element() -> None:
    root = tcx.parse(builders.tcx())
    assert root.tag == tcx.ROOT


def test_parse_rejects_malformed_xml() -> None:
    with pytest.raises(MalformedTCX, match="not well-formed"):
        tcx.parse(b"<TrainingCenterDatabase>")


def test_parse_rejects_a_non_tcx_root() -> None:
    with pytest.raises(MalformedTCX, match="expected a TCX"):
        tcx.parse(b"<gpx></gpx>")


def test_serialise_keeps_tcx_as_the_default_namespace() -> None:
    output = tcx.serialise(tcx.parse(builders.tcx(speeds=[1.0, 2.0, 3.0])))
    assert b"ns0:" not in output
    assert b'xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"' in output
    assert b"<Trackpoint>" in output


def test_serialise_emits_an_xml_declaration() -> None:
    assert tcx.serialise(tcx.parse(builders.tcx())).startswith(b"<?xml")


def test_activities_and_trackpoints_are_found() -> None:
    root = tcx.parse(builders.document(builders.activity(), builders.activity(start_offset=3600)))
    found = list(tcx.activities(root))
    assert len(found) == 2
    assert len(list(tcx.trackpoints(found[0]))) == 3


def test_has_position_distinguishes_outdoor_from_indoor() -> None:
    outdoor = next(tcx.activities(tcx.parse(builders.tcx())))
    indoor = next(tcx.activities(tcx.parse(builders.tcx(with_position=False))))
    assert tcx.has_position(outdoor)
    assert not tcx.has_position(indoor)


def test_label_uses_the_activity_id() -> None:
    activity = next(tcx.activities(tcx.parse(builders.tcx(activity_id="2024-05-05T07:00:00Z"))))
    assert tcx.label(activity) == "2024-05-05T07:00:00Z"


def test_label_falls_back_when_the_id_is_missing() -> None:
    activity = next(tcx.activities(tcx.parse(builders.tcx(include_id=False))))
    assert tcx.label(activity) == "<unidentified>"


def test_label_falls_back_when_the_id_is_blank() -> None:
    activity = next(tcx.activities(tcx.parse(builders.tcx(activity_id="   "))))
    assert tcx.label(activity) == "<unidentified>"


@pytest.mark.parametrize(("text", "expected"), [("10.5", 10.5), (" 12 ", 12.0), ("0", 0.0)])
def test_read_float_accepts_numbers(text: str, expected: float) -> None:
    assert tcx.read_float(element(text)) == expected


@pytest.mark.parametrize("text", ["", None, "  ", "ten", "1,5"])
def test_read_float_rejects_non_numbers(text: str | None) -> None:
    with pytest.raises(MalformedTCX, match="DistanceMeters is not a number"):
        tcx.read_float(element(text))


@pytest.mark.parametrize("text", ["nan", "inf", "-inf"])
def test_read_float_rejects_non_finite_numbers(text: str) -> None:
    with pytest.raises(MalformedTCX, match="DistanceMeters is not finite"):
        tcx.read_float(element(text))


def test_timestamps_collects_ids_lap_starts_and_trackpoint_times() -> None:
    found = tcx.timestamps(tcx.parse(builders.tcx(distances=[0.0, 100.0])))
    # Activity/Id, then Lap@StartTime, then one Time per trackpoint.
    assert len(found) == 4
    assert found[0] == builders.timestamp(0)
    assert found[2] == builders.timestamp(0)
    assert found[3] == builders.timestamp(10)


# --- GPS coverage ------------------------------------------------------------


def coverage(**kwargs) -> float:
    root = tcx.parse(builders.tcx(**kwargs))
    return tcx.gps_coverage(next(tcx.activities(root)))


def test_coverage_is_total_when_every_trackpoint_has_a_fix():
    assert coverage(distances=(0.0, 100.0, 200.0)) == pytest.approx(1.0)


def test_coverage_is_zero_without_any_fix():
    assert coverage(distances=(0.0, 100.0, 200.0), with_position=False) == 0.0


def test_coverage_is_measured_in_seconds_not_trackpoints():
    """Half the points but four fifths of the time: the answer must be 0.8."""
    root = tcx.parse(
        builders.document(
            builders.activity(
                distances=(None,) * 2 + (0.0, 100.0, 200.0),
                positions=[False] * 2 + [True] * 3,
            )
        )
    )
    # Two unlocked 10 s intervals out of a 40 s span.
    assert tcx.gps_coverage(next(tcx.activities(root))) == pytest.approx(0.5)


def test_coverage_of_a_partial_track_reflects_the_dropout():
    assert coverage(
        distances=(None,) * 8 + (0.0, 100.0), positions=[False] * 8 + [True] * 2
    ) == pytest.approx(1 / 9)


def test_coverage_of_an_empty_track_is_zero():
    assert coverage(distances=()) == 0.0


def test_coverage_of_a_single_trackpoint_is_total_when_it_has_a_fix():
    assert coverage(distances=(0.0,)) == 1.0


def test_coverage_of_a_single_trackpoint_is_zero_without_one():
    assert coverage(distances=(0.0,), with_position=False) == 0.0


def test_coverage_rejects_a_trackpoint_with_no_time():
    data = builders.tcx(distances=(0.0, 100.0)).replace(
        b"<Time>2024-01-01T09:00:00.000Z</Time>", b"", 1
    )
    root = tcx.parse(data)
    with pytest.raises(MalformedTCX, match="no Time"):
        tcx.gps_coverage(next(tcx.activities(root)))


def test_coverage_rejects_an_unparseable_timestamp():
    data = builders.tcx(distances=(0.0, 100.0)).replace(
        b"<Time>2024-01-01T09:00:00.000Z</Time>", b"<Time>the seventh of never</Time>", 1
    )
    root = tcx.parse(data)
    with pytest.raises(MalformedTCX, match="ISO 8601"):
        tcx.gps_coverage(next(tcx.activities(root)))
