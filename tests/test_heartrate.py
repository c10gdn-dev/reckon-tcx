"""Merging a heart-rate series back into a TCX.

The API's export carries no heart rate, so Reckon fetches the series separately
and writes it in. Two invariants matter more than the matching itself: timestamps
must be untouched, and data already in the file must not be overwritten.
"""

import datetime as dt
import xml.etree.ElementTree as ET

import pytest

import builders
from reckon.core import heartrate, tcx

START = dt.datetime(2024, 1, 1, 9, 0, 0, tzinfo=dt.UTC)


def at(seconds: float) -> dt.datetime:
    return START + dt.timedelta(seconds=seconds)


def document(**kwargs) -> bytes:
    kwargs.setdefault("distances", (0.0, 500.0, 1000.0))
    kwargs.setdefault("lap_distance_m", 930.0)
    # No heart rate, as the API's export arrives.
    kwargs.setdefault("with_heart_rate", False)
    return builders.tcx(**kwargs)


def rates(data: bytes) -> list[int | None]:
    """Heart rate per trackpoint, in order, None where absent."""
    root = tcx.parse(data)
    found: list[int | None] = []
    for activity in tcx.activities(root):
        for point in tcx.trackpoints(activity):
            element = point.find(heartrate.HEART_RATE_BPM)
            found.append(None if element is None else int(element.find(heartrate.VALUE).text))
    return found


# --- matching ---------------------------------------------------------------


def test_samples_are_written_onto_the_trackpoints() -> None:
    """builders spaces trackpoints ten seconds apart."""
    samples = [(at(0), 100), (at(10), 110), (at(20), 120)]
    result = heartrate.merge(document(), samples)
    assert rates(result.data) == [100, 110, 120]
    assert (result.matched, result.trackpoints) == (3, 3)


def test_the_nearest_sample_wins() -> None:
    samples = [(at(0), 100), (at(8), 108), (at(9), 109), (at(21), 121)]
    assert rates(heartrate.merge(document(), samples).data) == [100, 109, 121]


def test_a_sample_beyond_tolerance_is_not_used() -> None:
    """A gap in the series should read as missing, not as a stale value smeared over it.

    Trackpoints sit at 0, 10 and 20 s; one sample at 4 s is 16 s from the last.
    """
    assert rates(heartrate.merge(document(), [(at(4), 104)]).data) == [104, 104, None]


def test_the_tolerance_is_configurable() -> None:
    merged = heartrate.merge(document(), [(at(4), 104)], tolerance_s=20.0)
    assert rates(merged.data) == [104, 104, 104]


def test_a_gap_of_exactly_the_tolerance_is_accepted() -> None:
    """ "Within tolerance" reads as inclusive, and the boundary is worth pinning."""
    assert rates(heartrate.merge(document(), [(at(0), 100)], tolerance_s=10.0).data) == [
        100,
        100,
        None,
    ]
    assert rates(heartrate.merge(document(), [(at(0), 100)], tolerance_s=9.9).data) == [
        100,
        None,
        None,
    ]


def test_a_sample_before_the_first_trackpoint_still_matches() -> None:
    assert rates(heartrate.merge(document(), [(at(-3), 99)]).data) == [99, None, None]


def test_an_empty_series_changes_nothing() -> None:
    result = heartrate.merge(document(), [])
    assert rates(result.data) == [None, None, None]
    assert result.matched == 0


def test_samples_need_not_arrive_in_order() -> None:
    samples = [(at(20), 120), (at(0), 100), (at(10), 110)]
    assert rates(heartrate.merge(document(), samples).data) == [100, 110, 120]


# --- the invariants ---------------------------------------------------------


def test_no_timestamp_is_modified() -> None:
    """The same invariant `rescale` holds, and for the same reason."""
    original = document()
    before = tcx.timestamps(tcx.parse(original))
    merged = heartrate.merge(original, [(at(0), 100), (at(10), 110), (at(20), 120)])
    assert tcx.timestamps(tcx.parse(merged.data)) == before


def test_existing_heart_rate_is_never_overwritten() -> None:
    """An app export already has it; running this over one must change nothing."""
    original = document()
    once = heartrate.merge(original, [(at(0), 100), (at(10), 110), (at(20), 120)])
    twice = heartrate.merge(once.data, [(at(0), 55), (at(10), 55), (at(20), 55)])
    assert rates(twice.data) == [100, 110, 120]
    assert (twice.matched, twice.already_present) == (0, 3)


def test_the_merge_is_idempotent() -> None:
    samples = [(at(0), 100), (at(10), 110), (at(20), 120)]
    once = heartrate.merge(document(), samples)
    twice = heartrate.merge(once.data, samples)
    assert twice.data == once.data


def test_distance_and_position_survive_untouched() -> None:
    original = document()
    merged = heartrate.merge(original, [(at(0), 100)]).data
    for tag in ("DistanceMeters", "LatitudeDegrees", "AltitudeMeters"):
        assert original.count(tag.encode()) == merged.count(tag.encode())


# --- schema order -----------------------------------------------------------


def test_heart_rate_is_written_in_schema_order() -> None:
    """TCX is sequenced: HeartRateBpm belongs after DistanceMeters, before Extensions."""
    merged = heartrate.merge(document(speeds=(1.0, 1.0, 1.0)), [(at(0), 100)]).data
    point = next(tcx.trackpoints(next(tcx.activities(tcx.parse(merged)))))
    order = [child.tag.rpartition("}")[2] for child in point]
    assert order.index("HeartRateBpm") > order.index("DistanceMeters")
    assert order.index("HeartRateBpm") < order.index("Extensions")


def test_it_is_appended_when_nothing_follows_it() -> None:
    merged = heartrate.merge(document(distances=(None, None, None)), [(at(0), 100)]).data
    point = next(tcx.trackpoints(next(tcx.activities(tcx.parse(merged)))))
    assert [c.tag.rpartition("}")[2] for c in point][-1] == "HeartRateBpm"


def test_the_result_is_still_a_valid_document() -> None:
    merged = heartrate.merge(document(), [(at(0), 100)]).data
    ET.fromstring(merged)
    assert tcx.lap_distance_total(next(tcx.activities(tcx.parse(merged)))) == 930.0


# --- reporting --------------------------------------------------------------


def test_coverage_reports_how_much_of_the_track_got_heart_rate() -> None:
    result = heartrate.merge(document(), [(at(4), 104), (at(5), 105)])
    assert result.coverage == pytest.approx(2 / 3)
    assert result.samples == 2


def test_coverage_counts_what_was_already_there() -> None:
    once = heartrate.merge(document(), [(at(0), 100), (at(10), 110), (at(20), 120)])
    twice = heartrate.merge(once.data, [])
    assert twice.coverage == 1.0


def test_coverage_of_a_document_with_no_trackpoints_is_zero() -> None:
    empty = builders.document(builders.activity(distances=(), with_heart_rate=False))
    result = heartrate.merge(empty, [(at(0), 100)])
    assert (result.coverage, result.trackpoints) == (0.0, 0)


def test_a_trackpoint_without_a_time_is_skipped_not_fatal() -> None:
    original = document().replace(b"<Time>", b"<Moment>", 1).replace(b"</Time>", b"</Moment>", 1)
    result = heartrate.merge(original, [(at(0), 100), (at(10), 110), (at(20), 120)])
    assert rates(result.data) == [None, 110, 120]


# --- the lap average --------------------------------------------------------
#
# The fallback when the per-second series is out of reach: the exercise summary
# carries an average the activity scope can read, where the series needs a
# restricted scope and Google's verification behind it.


def lap_average(data: bytes) -> list[int | None]:
    root = tcx.parse(data)
    found: list[int | None] = []
    for activity in tcx.activities(root):
        for lap in activity.iter(tcx.LAP):
            e = lap.find(heartrate.AVERAGE_HEART_RATE_BPM)
            found.append(None if e is None else int(e.find(heartrate.VALUE).text))
    return found


def test_the_average_is_written_onto_the_lap() -> None:
    data, refused = heartrate.set_average(document(), 146)
    assert refused is None
    assert lap_average(data) == [146]


def test_it_goes_in_schema_order() -> None:
    """After Calories, before Intensity. A misplaced element is invalid XML."""
    data, _ = heartrate.set_average(document(), 146)
    lap = next(tcx.activities(tcx.parse(data))).find(tcx.LAP)
    order = [c.tag.rpartition("}")[2] for c in lap]
    assert order.index("AverageHeartRateBpm") > order.index("Calories")
    assert order.index("AverageHeartRateBpm") < order.index("Intensity")


def test_an_existing_average_is_not_replaced() -> None:
    once, _ = heartrate.set_average(document(), 146)
    twice, _ = heartrate.set_average(once, 99)
    assert lap_average(twice) == [146]
    assert twice == once, "and the document is untouched"


def test_a_multi_lap_activity_is_refused_with_a_reason() -> None:
    """An activity average is not a lap average, and pretending otherwise invents data."""
    data, refused = heartrate.set_average(document(laps=3), 146)
    assert refused is not None
    assert "not a lap average" in refused
    assert lap_average(data) == [None, None, None]


def test_timestamps_survive_the_lap_average() -> None:
    original = document()
    before = tcx.timestamps(tcx.parse(original))
    data, _ = heartrate.set_average(original, 146)
    assert tcx.timestamps(tcx.parse(data)) == before


def test_the_result_is_still_valid() -> None:
    data, _ = heartrate.set_average(document(), 146)
    ET.fromstring(data)
    assert tcx.lap_distance_total(next(tcx.activities(tcx.parse(data)))) == 930.0


# --- building a track the API will not export -------------------------------
#
# Verified against the live API on 2026-09-12: a yoga session and a
# walk-plus-train both came back from `:exportExerciseTcx` as two bare `<Time>`
# trackpoints with no heart rate, where the phone app's export of the same walk
# has 373. Merging onto two trackpoints gives two readings, and Relative Effort
# needs time in zones.

SKELETON = (
    b'<?xml version="1.0"?>'
    b'<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
    b'<Activities><Activity Sport="Other"><Id>2026-09-11T07:00:00Z</Id>'
    b'<Lap StartTime="2026-09-11T07:00:00Z"><TotalTimeSeconds>600.0</TotalTimeSeconds>'
    b"<DistanceMeters>0.0</DistanceMeters><Intensity>Active</Intensity>"
    b"<TriggerMethod>Manual</TriggerMethod><Track>"
    b"<Trackpoint><Time>2026-09-11T07:00:00Z</Time></Trackpoint>"
    b"<Trackpoint><Time>2026-09-11T07:10:00Z</Time></Trackpoint>"
    b"</Track></Lap></Activity></Activities></TrainingCenterDatabase>"
)

SKELETON_START = dt.datetime(2026, 9, 11, 7, 0, tzinfo=dt.UTC)


def readings(count: int, *, every: int = 60, offset: int = 0) -> list[tuple[dt.datetime, int]]:
    return [
        (SKELETON_START + dt.timedelta(seconds=offset + every * i), 70 + i) for i in range(count)
    ]


def built(data: bytes) -> list[tuple[str, int | None]]:
    """Every trackpoint as (time, bpm), in document order."""
    activity = next(tcx.activities(tcx.parse(data)))
    found = []
    for point in tcx.trackpoints(activity):
        element = point.find(heartrate.HEART_RATE_BPM)
        value = None if element is None else int(element.findtext(heartrate.VALUE))
        found.append((point.findtext(tcx.TIME), value))
    return found


def test_build_creates_a_trackpoint_for_every_sample() -> None:
    """Offset past the tolerance, or the first sample annotates the skeleton
    point instead of creating one — which the next test covers."""
    result = heartrate.build(SKELETON, readings(5, every=100, offset=30))

    assert result.created == 5
    assert [bpm for _, bpm in built(result.data)] == [None, 70, 71, 72, 73, 74, None]


def test_build_keeps_the_bounding_trackpoints_so_elapsed_time_survives() -> None:
    """Strava reads elapsed from the trackpoint span, not from TotalTimeSeconds.

    Dropping the skeleton's two points would shorten the activity by however
    long the watch waited before its first reading.
    """
    result = heartrate.build(SKELETON, readings(3, every=100, offset=120))

    times = [moment for moment, _ in built(result.data)]
    assert times[0] == "2026-09-11T07:00:00Z"
    assert times[-1] == "2026-09-11T07:10:00Z"


def test_build_annotates_an_existing_trackpoint_rather_than_duplicating_it() -> None:
    """A sample landing on the skeleton's own timestamp is not a second point."""
    result = heartrate.build(SKELETON, [(SKELETON_START, 66)])

    assert result.created == 0
    assert result.annotated == 1
    assert built(result.data) == [("2026-09-11T07:00:00Z", 66), ("2026-09-11T07:10:00Z", None)]


def test_build_emits_trackpoints_in_time_order() -> None:
    shuffled = list(reversed(readings(4, every=120, offset=30)))

    result = heartrate.build(SKELETON, shuffled)

    times = [moment for moment, _ in built(result.data)]
    assert times == sorted(times)


def test_build_drops_a_sample_from_outside_the_activity(recwarn) -> None:
    """Dropped rather than clamped: clamping invents a reading at a time it was
    not taken, which is the line this function exists to stay on."""
    before = [(SKELETON_START - dt.timedelta(minutes=5), 99)]
    after = [(SKELETON_START + dt.timedelta(minutes=20), 98)]

    result = heartrate.build(SKELETON, before + readings(2, every=120, offset=60) + after)

    assert result.outside == 2
    assert 99 not in [bpm for _, bpm in built(result.data)]
    assert 98 not in [bpm for _, bpm in built(result.data)]


def test_build_leaves_an_activity_with_gps_to_merge() -> None:
    """Where there is a track, nothing is created — `merge` annotates it."""
    with_gps = builders.tcx(distances=(0.0, 500.0), with_heart_rate=False)

    result = heartrate.build(with_gps, readings(5, every=5))

    assert result.created == 0
    assert result.annotated == 0
    assert result.skipped_activities == 1
    # Compared as content rather than bytes: the document is re-serialised either
    # way, and the claim is that nothing in it changed.
    assert built(result.data) == built(with_gps)


def test_build_never_touches_the_lap_distance() -> None:
    """The stride total is already the right answer; building adds no distance."""
    result = heartrate.build(SKELETON, readings(3, every=120, offset=30))

    activity = next(tcx.activities(tcx.parse(result.data)))
    assert tcx.lap_distance_total(activity) == 0.0


def test_build_creates_trackpoints_with_no_position_and_no_distance() -> None:
    """The shape the phone app writes, which Strava has already accepted."""
    result = heartrate.build(SKELETON, readings(2, every=120, offset=30))

    activity = next(tcx.activities(tcx.parse(result.data)))
    created = list(tcx.trackpoints(activity))[1]
    assert created.find(tcx.POSITION) is None
    assert created.find(tcx.DISTANCE_METERS) is None


def test_build_with_no_samples_changes_nothing() -> None:
    result = heartrate.build(SKELETON, [])

    assert (result.created, result.annotated) == (0, 0)
    assert built(result.data) == [("2026-09-11T07:00:00Z", None), ("2026-09-11T07:10:00Z", None)]


def test_build_leaves_a_reading_that_is_already_there_alone() -> None:
    already = SKELETON.replace(
        b"<Trackpoint><Time>2026-09-11T07:00:00Z</Time></Trackpoint>",
        b"<Trackpoint><Time>2026-09-11T07:00:00Z</Time>"
        b"<HeartRateBpm><Value>55</Value></HeartRateBpm></Trackpoint>",
    )

    result = heartrate.build(already, [(SKELETON_START, 66)])

    assert result.annotated == 0
    assert built(result.data)[0] == ("2026-09-11T07:00:00Z", 55)


def test_build_skips_a_lap_with_no_track() -> None:
    """Nothing to build into, and nothing to be confused by."""
    no_track = SKELETON.replace(
        b"<Track><Trackpoint><Time>2026-09-11T07:00:00Z</Time></Trackpoint>"
        b"<Trackpoint><Time>2026-09-11T07:10:00Z</Time></Trackpoint></Track>",
        b"",
    )

    result = heartrate.build(no_track, readings(3, every=120, offset=30))

    assert result.created == 0


def test_build_skips_a_lap_whose_track_is_empty() -> None:
    empty = SKELETON.replace(
        b"<Trackpoint><Time>2026-09-11T07:00:00Z</Time></Trackpoint>"
        b"<Trackpoint><Time>2026-09-11T07:10:00Z</Time></Trackpoint>",
        b"",
    )

    result = heartrate.build(empty, readings(3, every=120, offset=30))

    assert result.created == 0


def test_build_leaves_a_track_it_cannot_order_alone() -> None:
    """A trackpoint with no time cannot be placed, and the track is rewritten in
    time order — so moving it somewhere arbitrary is the alternative."""
    odd = SKELETON.replace(
        b"<Trackpoint><Time>2026-09-11T07:10:00Z</Time></Trackpoint>",
        b"<Trackpoint><Time>2026-09-11T07:10:00Z</Time></Trackpoint><Trackpoint/>",
    )

    result = heartrate.build(odd, readings(3, every=120, offset=30))

    assert result.created == 0
    assert len(built(result.data)) == 3


def test_build_keeps_a_1_hz_series_at_1_hz() -> None:
    """The shape it exists to reproduce, and the bug that stopped it.

    `tolerance_s` was measured against the trackpoints being *created* as well as
    the ones already there, so it silently became a minimum spacing: one point at
    t suppressed every sample to t+10, and a 1 Hz series emerged at one point per
    11 s. 91% of real measurements discarded by a function written to preserve
    them — one mechanism doing two jobs, for the eighth time in this codebase.
    """
    every_second = [(SKELETON_START + dt.timedelta(seconds=i), 70 + i % 30) for i in range(601)]

    result = heartrate.build(SKELETON, every_second)

    # 601 samples, less the 11 within 10 s of each skeleton trackpoint (0-10 s
    # and 590-600 s inclusive) = 579 created, with the two skeleton points
    # annotated instead. Exact rather than approximate: a bound would have passed
    # at one point per 11 s, which is the bug.
    assert (result.created, result.annotated) == (579, 2)
    times = [moment for moment, _ in built(result.data)]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_build_does_not_write_two_trackpoints_at_one_instant() -> None:
    """Duplicate sample timestamps are a shape the API could return."""
    moment = SKELETON_START + dt.timedelta(seconds=120)

    result = heartrate.build(SKELETON, [(moment, 70), (moment, 99)])

    assert result.created == 1
