"""Corpus measurement, against synthetic inputs with known answers."""

import re

import pytest

import builders
from reckon.core.analyse import MOVING_SPEED_MS, analyse_tcx, summarise
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
    s = stats(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

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
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0).replace(
        b"<TotalTimeSeconds>600.0</TotalTimeSeconds>", b"", 1
    )

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
