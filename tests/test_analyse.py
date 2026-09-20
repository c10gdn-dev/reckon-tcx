"""Corpus measurement, against synthetic inputs with known answers."""

import re

import pytest

import builders
from reckon.core.analyse import (
    MOVING_SPEED_MS,
    ActivityStats,
    analyse_tcx,
    summarise,
    threshold_evidence,
)
from reckon.core.errors import MalformedTCX
from reckon.core.rescale import SkipReason


def stats(**kwargs):
    return analyse_tcx(builders.tcx(**kwargs))


def test_measures_a_straightforward_activity():
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    assert s.sport == "Running"
    assert s.trackpoints == 3
    assert s.trackpoints_with_gps == 3
    assert s.lap_distance_m == pytest.approx(900.0)
    assert s.stream_distance_m == pytest.approx(1000.0)
    assert s.factor == pytest.approx(0.9)
    assert s.corrected is True
    assert s.gps_coverage == pytest.approx(1.0)
    assert s.skipped == ()


def test_inflation_is_the_reciprocal_of_the_factor():
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=800.0)

    assert s.inflation == pytest.approx(0.25)


def test_inflation_is_absent_when_nothing_was_corrected():
    s = stats(with_position=False)

    assert s.factor is None
    assert s.inflation is None
    assert s.corrected is False
    assert s.skipped == (SkipReason.NO_GPS,)


def test_elapsed_and_lap_time_are_reported_separately():
    """The lap's own duration is not the span of its trackpoints, and can exceed it."""
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0, lap_total_time_s=600.0)

    assert s.elapsed_s == pytest.approx(20.0)  # three points, ten seconds apart
    assert s.lap_total_time_s == pytest.approx(600.0)


def test_lead_in_is_the_wait_for_a_first_fix():
    s = stats(
        distances=(None, None, 0.0, 100.0),
        positions=[False, False, True, True],
        lap_distance_m=95.0,
    )

    assert s.lead_in_s == pytest.approx(20.0)


def test_lead_in_is_absent_without_any_fix():
    assert stats(with_position=False).lead_in_s is None


def test_start_lag_compares_the_first_trackpoint_with_the_activity_id():
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0, start_offset=0)

    assert s.start_lag_s == pytest.approx(0.0)


def test_start_lag_is_absent_when_the_id_is_not_a_timestamp():
    s = analyse_tcx(
        builders.tcx(distances=(0.0, 100.0), lap_distance_m=95.0, activity_id="not-a-time")
    )

    assert s.start_lag_s is None


def test_start_lag_is_absent_when_there_is_no_id():
    s = analyse_tcx(builders.tcx(distances=(0.0, 100.0), lap_distance_m=95.0, include_id=False))

    assert s.start_lag_s is None


def test_a_file_with_no_total_to_correct_to_is_measured_anyway():
    """Analysis reports the problem; it does not abandon the file."""
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=None)

    assert s.no_target is True
    assert s.factor is None
    assert s.corrected is False
    assert s.stream_distance_m == pytest.approx(1000.0)
    assert s.wiggle is not None


def test_wiggle_is_one_for_a_track_with_no_high_frequency_noise():
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    assert s.wiggle == pytest.approx(1.0, abs=1e-6)


def test_wiggle_is_absent_with_fewer_than_two_fixes():
    s = stats(distances=(0.0,), lap_distance_m=1.0)

    assert s.wiggle is None


def test_moving_time_moves_with_the_factor():
    """Scaling the stream scales the speeds, so the threshold catches differently."""
    # Steps of 10 m over 10 s each: exactly 1.0 m/s, comfortably above the
    # threshold until the factor drops it below.
    s = stats(distances=(0.0, 10.0, 20.0, 30.0), lap_distance_m=12.0)

    assert s.moving_before_s > 0
    assert s.moving_after_s < s.moving_before_s
    assert s.moving_delta_s < 0


def test_moving_time_ignores_samples_below_the_threshold():
    slow = 1.0 * MOVING_SPEED_MS * 10 / 2  # half the threshold over a ten second step
    s = stats(distances=(0.0, slow, slow * 2), lap_distance_m=slow * 2)

    assert s.moving_before_s == 0.0


def test_a_file_with_no_activity_is_rejected():
    with pytest.raises(MalformedTCX, match="no Activity"):
        analyse_tcx(builders.document())


def test_a_file_with_no_trackpoints_is_rejected():
    with pytest.raises(MalformedTCX, match="no trackpoints"):
        analyse_tcx(builders.tcx(distances=()))


def test_a_trackpoint_without_a_time_is_rejected():
    data = builders.tcx(distances=(0.0, 100.0)).replace(
        b"<Time>2024-01-01T09:00:00.000Z</Time>", b"", 1
    )

    with pytest.raises(MalformedTCX, match="no Time"):
        analyse_tcx(data)


def test_a_position_missing_a_coordinate_is_rejected():
    data = builders.tcx(distances=(0.0, 100.0)).replace(
        b"<LatitudeDegrees>51.5074</LatitudeDegrees>", b"", 1
    )

    with pytest.raises(MalformedTCX, match="latitude or longitude"):
        analyse_tcx(data)


def test_a_lap_without_a_recorded_time_contributes_nothing():
    data = builders.tcx(
        distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0, lap_total_time_s=600.0
    ).replace(b"<TotalTimeSeconds>600.0</TotalTimeSeconds>", b"", 1)

    assert analyse_tcx(data).lap_total_time_s == 0.0


def test_wiggle_is_absent_when_the_track_never_moves():
    """Every fix at the same point gives a zero-length path to divide by."""
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)
    for tag in (b"LatitudeDegrees", b"LongitudeDegrees"):
        data = re.sub(b"<%s>[^<]*</%s>" % (tag, tag), b"<%s>51.5</%s>" % (tag, tag), data)

    assert analyse_tcx(data).wiggle is None


def test_trackpoints_sharing_a_timestamp_are_skipped_when_timing_movement():
    """A zero-length interval has no speed; it must not divide by zero."""
    data = builders.tcx(distances=(0.0, 10.0, 20.0, 30.0), lap_distance_m=28.0).replace(
        b"<Time>2024-01-01T09:00:10.000Z</Time>", b"<Time>2024-01-01T09:00:00.000Z</Time>", 1
    )

    stats = analyse_tcx(data)

    assert stats.moving_before_s >= 0


# --- summarising -------------------------------------------------------------


def test_summary_aggregates_the_corrected_files():
    measured = [
        stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0),
        stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=800.0),
        stats(with_position=False),
    ]

    summary = summarise(measured)

    assert summary.files == 3
    assert summary.corrected == 2
    assert summary.factor_min == pytest.approx(0.8)
    assert summary.factor_max == pytest.approx(0.9)
    assert summary.factor_mean == pytest.approx(0.85)
    assert summary.factor_stdev is not None
    assert summary.skipped == ((SkipReason.NO_GPS, 1),)


def test_summary_of_a_single_file_has_no_stdev():
    summary = summarise([stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)])

    assert summary.corrected == 1
    assert summary.factor_stdev is None


def test_summary_of_nothing_corrected_reports_no_factors():
    summary = summarise([stats(with_position=False)])

    assert summary.corrected == 0
    assert summary.factor_mean is None
    assert summary.factor_min is None
    assert summary.factor_max is None


def test_summary_of_an_empty_corpus():
    summary = summarise([])

    assert summary.files == 0
    assert summary.worst_moving_delta_s == 0.0
    assert summary.skipped == ()


# --- threshold evidence -------------------------------------------------------
#
# The partitioning is tested against hand-built ActivityStats rather than real
# files, because the point of each case is one measurement sitting a known
# distance from a bound, and building a TCX that lands exactly on 3.0 would
# obscure that.


def measured(name="a", **fields):
    """One (name, stats) pair, with only the field under test moved."""
    base = {
        "sport": "Running",
        "trackpoints": 3,
        "trackpoints_with_gps": 3,
        "elapsed_s": 20.0,
        "lap_total_time_s": 20.0,
        "lap_distance_m": 900.0,
        "stream_distance_m": 1000.0,
        "haversine_m": 1000.0,
        "factor": 0.9,
        "gps_coverage": 1.0,
        "gap_fraction": 0.0,
        "unrecorded_fraction": 0.0,
        "median_interval_s": 10.0,
        "max_interval_s": 10.0,
        "wiggle": 1.0,
        "lead_in_s": 0.0,
        "start_lag_s": 0.0,
        "moving_before_s": 20.0,
        "moving_after_s": 20.0,
        "skipped": (),
    }
    return (name, ActivityStats(**{**base, **fields}))


def by_name(evidence):
    return {item.name: item for item in evidence}


def test_reports_every_constant_the_comments_cite():
    report = by_name(threshold_evidence([measured()]))

    assert set(report) == {
        "MIN_GPS_COVERAGE",
        "MAX_CREDIBLE_FACTOR",
        "DEFAULT_TOLERANCE",
        "MAX_GAP_FRACTION",
        "MAX_UNRECORDED_FRACTION",
        "GAP_MULTIPLE",
    }


def test_clearance_is_the_distance_between_the_nearest_file_each_side():
    report = by_name(
        threshold_evidence(
            [
                measured("low", gap_fraction=0.01),
                measured("nearer", gap_fraction=0.04),
                measured("over", gap_fraction=0.09),
                measured("far", gap_fraction=0.50),
            ]
        )
    )
    gaps = report["MAX_GAP_FRACTION"]

    assert gaps.population == 4
    assert gaps.below[0][0] == "nearer", "below sorts towards the bound"
    assert gaps.above[0][0] == "over", "above sorts towards the bound"
    assert gaps.clearance == pytest.approx(0.05)


def test_clearance_is_none_when_one_side_is_empty():
    """An unoccupied side is reported as absent, never as a wide margin."""
    report = by_name(threshold_evidence([measured(gap_fraction=0.01)]))

    assert report["MAX_GAP_FRACTION"].above == ()
    assert report["MAX_GAP_FRACTION"].clearance is None


def test_a_value_exactly_on_the_bound_counts_as_below():
    """Seventeen real files sit exactly on GAP_MULTIPLE; none of them is a gap."""
    report = by_name(threshold_evidence([measured(median_interval_s=1.0, max_interval_s=3.0)]))

    assert report["GAP_MULTIPLE"].below[0][1] == pytest.approx(3.0)
    assert report["GAP_MULTIPLE"].above == ()


def test_a_file_carrying_no_gps_is_left_out_of_the_coverage_population():
    """A yoga session says nothing about GPS coverage and must not sit at zero."""
    report = by_name(
        threshold_evidence([measured("run"), measured("yoga", trackpoints_with_gps=0)])
    )

    assert report["MIN_GPS_COVERAGE"].population == 1
    assert report["MAX_GAP_FRACTION"].population == 2, "gaps apply to every file"


def test_a_file_that_was_not_corrected_has_no_factor_to_place():
    report = by_name(threshold_evidence([measured("run"), measured("skipped", factor=None)]))

    assert report["DEFAULT_TOLERANCE"].population == 1
    assert report["MAX_CREDIBLE_FACTOR"].population == 1


def test_an_incomplete_track_is_left_out_of_the_credible_factor_population():
    """The constant's claim is about complete tracks, so it measures only those."""
    report = by_name(threshold_evidence([measured("whole"), measured("partial", gps_coverage=0.9)]))

    assert report["MAX_CREDIBLE_FACTOR"].population == 1
    assert report["MAX_CREDIBLE_FACTOR"].below[0][0] == "whole"


def test_gap_multiple_is_measured_against_each_files_own_median():
    """A 10 s recorder sampling normally must not read as a gap."""
    report = by_name(
        threshold_evidence(
            [
                measured("fast", median_interval_s=1.0, max_interval_s=4.0),
                measured("slow", median_interval_s=10.0, max_interval_s=20.0),
            ]
        )
    )

    assert dict(report["GAP_MULTIPLE"].above)["fast"] == pytest.approx(4.0)
    assert dict(report["GAP_MULTIPLE"].below)["slow"] == pytest.approx(2.0)


def test_a_file_with_no_interval_to_measure_is_left_out():
    """A single trackpoint has no sampling rate, so it cannot have a gap ratio."""
    report = by_name(threshold_evidence([measured(median_interval_s=0.0)]))

    assert report["GAP_MULTIPLE"].population == 0
    assert report["GAP_MULTIPLE"].clearance is None


def test_tolerance_is_measured_as_distance_from_one_in_both_directions():
    report = by_name(
        threshold_evidence([measured("under", factor=0.8), measured("over", factor=1.2)])
    )

    assert dict(report["DEFAULT_TOLERANCE"].below) == {
        "under": pytest.approx(0.2),
        "over": pytest.approx(0.2),
    }


def test_the_new_measurements_reach_activity_stats():
    """The three fields the threshold report needs are populated from a real parse."""
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0, spacing_seconds=10)

    assert s.median_interval_s == pytest.approx(10.0)
    assert s.max_interval_s == pytest.approx(10.0)
    assert s.unrecorded_fraction == pytest.approx(0.0)
