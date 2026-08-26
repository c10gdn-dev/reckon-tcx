"""The transform itself: the factor, the guards, and what must not move.

The invariant that matters most here is temporal. `test_timestamps_survive_*`
compares `tcx.timestamps()` before and after; every other assertion in this file
is about arithmetic, and arithmetic is recoverable. A shifted timestamp is not.
"""

import pytest

import builders
from reckon.core import tcx
from reckon.core.errors import MalformedTCX, MissingTarget, ToleranceExceeded
from reckon.core.rescale import ToleranceAction, rescale_tcx


def distances(data: bytes) -> list[float]:
    """Every Trackpoint/DistanceMeters value in a document, in order."""
    root = tcx.parse(data)
    return [
        tcx.read_float(element)
        for point in root.iter(tcx.TRACKPOINT)
        for element in point.findall(tcx.DISTANCE_METERS)
    ]


def texts(data: bytes, tag: str, namespace: str = tcx.TCX_NS) -> list[str]:
    """Raw text of every element with `tag`, so formatting can be asserted on."""
    root = tcx.parse(data)
    return [(e.text or "") for e in root.iter(tcx.qn(namespace, tag))]


# --- the happy path ----------------------------------------------------------


def test_scales_distance_stream_to_target():
    result = rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 900.0)

    assert result.modified is True
    assert result.factor == pytest.approx(0.9)
    assert result.gps_total_m == pytest.approx(1000.0)
    assert result.target_m == pytest.approx(900.0)
    assert result.result_total_m == pytest.approx(900.0)
    assert distances(result.data) == pytest.approx([0.0, 450.0, 900.0])


def test_trackpoint_count_counts_every_point_not_just_scaled_ones():
    result = rescale_tcx(builders.tcx(distances=(0.0, 500.0, None, 1000.0)), 900.0)

    assert result.trackpoint_count == 4
    assert result.warnings == ()


def test_scales_speed_and_lap_totals_not_only_trackpoints():
    data = builders.tcx(
        distances=(0.0, 500.0, 1000.0),
        speeds=(0.0, 2.5, 5.0),
        lap_distance_m=1000.0,
        max_speed=5.0,
        avg_speed=2.5,
    )

    result = rescale_tcx(data, 500.0, tolerance=0.6)

    assert result.factor == pytest.approx(0.5)
    assert texts(result.data, "MaximumSpeed") == ["2.5"]
    assert texts(result.data, "AvgSpeed", tcx.AX_NS) == ["1.25"]
    assert texts(result.data, "Speed", tcx.AX_NS) == ["0", "1.25", "2.5"]
    # Lap and Trackpoint DistanceMeters alike.
    assert texts(result.data, "DistanceMeters") == ["500", "0", "250", "500"]


def test_result_is_formatted_without_float_noise():
    result = rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 2000.0, tolerance=1.0)

    assert texts(result.data, "DistanceMeters") == ["0", "1000", "2000"]


def test_multiple_activities_share_one_factor_from_their_combined_total():
    data = builders.document(
        builders.activity(distances=(0.0, 400.0), activity_id="first"),
        builders.activity(distances=(0.0, 600.0), activity_id="second", start_offset=3600),
    )

    result = rescale_tcx(data, 500.0, tolerance=0.6)

    assert result.gps_total_m == pytest.approx(1000.0)
    assert result.factor == pytest.approx(0.5)
    assert distances(result.data) == pytest.approx([0.0, 200.0, 0.0, 300.0])


def test_multiple_laps_are_all_scaled():
    data = builders.tcx(distances=(0.0, 250.0, 500.0, 1000.0), laps=2)

    result = rescale_tcx(data, 500.0, tolerance=0.6)

    assert distances(result.data) == pytest.approx([0.0, 125.0, 250.0, 500.0])


# --- the temporal invariant --------------------------------------------------


def test_timestamps_survive_a_rescale_unchanged():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), laps=2)
    before = tcx.timestamps(tcx.parse(data))

    result = rescale_tcx(data, 900.0)

    assert tcx.timestamps(tcx.parse(result.data)) == before
    assert len(before) > 3  # Id, Lap StartTimes and Trackpoint Times, not an empty list.


def test_positions_and_altitudes_survive_a_rescale_unchanged():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0))
    result = rescale_tcx(data, 900.0)

    for tag in ("LatitudeDegrees", "LongitudeDegrees", "AltitudeMeters"):
        assert texts(result.data, tag) == texts(data, tag), tag


def test_heart_rate_survives_a_rescale_unchanged():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0))
    result = rescale_tcx(data, 900.0)

    assert texts(result.data, "Value") == texts(data, "Value")


# --- guards ------------------------------------------------------------------


@pytest.mark.parametrize("target", [0.0, -1.0, float("nan"), float("inf")])
def test_rejects_a_target_that_is_not_a_positive_finite_distance(target):
    with pytest.raises(ValueError, match="positive number of metres"):
        rescale_tcx(builders.tcx(), target)


def test_activity_without_gps_is_left_alone():
    data = builders.tcx(with_position=False)

    result = rescale_tcx(data, 900.0)

    assert result.modified is False
    assert result.data == data
    assert result.factor == 1.0
    assert result.gps_total_m == 0.0
    assert result.trackpoint_count == 3
    assert any("no GPS positions" in w for w in result.warnings)
    assert any("no activity carries a GPS distance stream" in w for w in result.warnings)


def test_activity_without_a_distance_stream_is_left_alone():
    data = builders.tcx(distances=(None, None, None))

    result = rescale_tcx(data, 900.0)

    assert result.modified is False
    assert result.data == data
    # Every trackpoint in the file is counted, scaled or not: the number
    # describes the input, not what the transform happened to touch.
    assert result.trackpoint_count == 3
    assert any("no Trackpoint/DistanceMeters" in w for w in result.warnings)


def test_zero_distance_total_is_left_alone_rather_than_dividing_by_it():
    data = builders.tcx(distances=(0.0, 0.0, 0.0))

    result = rescale_tcx(data, 900.0)

    assert result.modified is False
    assert result.data == data
    assert any("GPS distance total is zero" in w for w in result.warnings)


def test_empty_track_is_left_alone():
    data = builders.tcx(distances=())

    result = rescale_tcx(data, 900.0)

    assert result.modified is False
    assert result.trackpoint_count == 0


def test_non_monotonic_stream_warns_but_still_scales():
    data = builders.tcx(distances=(0.0, 500.0, 400.0))

    result = rescale_tcx(data, 200.0, tolerance=0.6)

    assert result.modified is True
    assert result.factor == pytest.approx(0.5)
    assert any("not monotonic" in w for w in result.warnings)
    # Multiplication preserves the ordering it was given, including backwards.
    assert distances(result.data) == pytest.approx([0.0, 250.0, 200.0])


def test_unidentified_activity_is_named_rather_than_crashing_the_warning():
    data = builders.tcx(with_position=False, include_id=False)

    result = rescale_tcx(data, 900.0)

    assert any("<unidentified>" in w for w in result.warnings)


def test_malformed_distance_value_is_reported_as_malformed_tcx():
    data = builders.tcx().replace(b"<DistanceMeters>500.0<", b"<DistanceMeters>bananas<")

    with pytest.raises(MalformedTCX, match="not a number"):
        rescale_tcx(data, 900.0)


# --- tolerance ---------------------------------------------------------------


def test_factor_outside_tolerance_aborts_by_default():
    with pytest.raises(ToleranceExceeded) as caught:
        rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 500.0)

    error = caught.value
    assert error.factor == pytest.approx(0.5)
    assert error.gps_total_m == pytest.approx(1000.0)
    assert error.target_m == pytest.approx(500.0)
    assert error.tolerance == pytest.approx(0.2)
    assert "--on-tolerance clamp|proceed" in str(error)


def test_factor_below_tolerance_can_be_clamped():
    result = rescale_tcx(
        builders.tcx(distances=(0.0, 500.0, 1000.0)),
        500.0,
        on_tolerance=ToleranceAction.CLAMP,
    )

    assert result.factor == pytest.approx(0.8)
    assert result.result_total_m == pytest.approx(800.0)
    assert any("clamped to 0.8000" in w for w in result.warnings)


def test_factor_above_tolerance_can_be_clamped():
    result = rescale_tcx(
        builders.tcx(distances=(0.0, 500.0, 1000.0)),
        5000.0,
        on_tolerance=ToleranceAction.CLAMP,
    )

    assert result.factor == pytest.approx(1.2)


def test_factor_outside_tolerance_can_proceed_unclamped():
    result = rescale_tcx(
        builders.tcx(distances=(0.0, 500.0, 1000.0)),
        500.0,
        on_tolerance=ToleranceAction.PROCEED,
    )

    assert result.factor == pytest.approx(0.5)
    assert result.result_total_m == pytest.approx(500.0)
    assert any("outside tolerance" in w and "proceeding" in w for w in result.warnings)


def test_factor_exactly_on_the_tolerance_bound_is_allowed():
    result = rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 800.0, tolerance=0.2)

    assert result.factor == pytest.approx(0.8)


# --- taking the target from the file ---------------------------------------


def test_target_defaults_to_the_files_own_lap_distance():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    result = rescale_tcx(data)

    assert result.target_m == pytest.approx(900.0)
    assert result.factor == pytest.approx(0.9)
    assert distances(result.data) == pytest.approx([0.0, 450.0, 900.0])


def test_explicit_target_overrides_the_files_own():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    result = rescale_tcx(data, 800.0)

    assert result.target_m == pytest.approx(800.0)


def test_lap_totals_are_summed_across_laps_and_activities():
    data = builders.document(
        builders.activity(distances=(0.0, 400.0), lap_distance_m=380.0),
        builders.activity(distances=(0.0, 600.0), lap_distance_m=570.0, start_offset=3600),
    )

    result = rescale_tcx(data, tolerance=0.6)

    assert result.target_m == pytest.approx(950.0)
    assert result.gps_total_m == pytest.approx(1000.0)


def test_missing_lap_distance_is_reported_rather_than_guessed():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=None)

    with pytest.raises(MissingTarget, match="pass an explicit distance"):
        rescale_tcx(data)


def test_zero_lap_distance_is_not_a_usable_target():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=0.0)

    with pytest.raises(MissingTarget):
        rescale_tcx(data)


def test_no_gps_file_needs_no_target_at_all():
    """A yoga session has nothing to scale, so it must not demand a distance."""
    data = builders.tcx(with_position=False, lap_distance_m=0.0)

    result = rescale_tcx(data)

    assert result.modified is False
    assert result.data == data
    assert result.target_m is None


def test_an_explicit_bad_target_is_still_rejected_even_with_nothing_to_scale():
    """An explicit argument is the caller's error and is never second-guessed."""
    with pytest.raises(ValueError, match="positive number of metres"):
        rescale_tcx(builders.tcx(with_position=False), -1.0)


# --- Lap totals are the target, not another thing to scale -------------------


def test_lap_total_survives_untouched_when_it_is_the_target():
    """Fitbit's own figure is the ground truth; scaling it would destroy it."""
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    result = rescale_tcx(data)

    assert texts(result.data, "DistanceMeters") == ["900", "0", "450", "900"]


def test_output_is_self_consistent_lap_equals_stream():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    result = rescale_tcx(data)
    values = texts(result.data, "DistanceMeters")

    assert values[0] == values[-1] == "900"


def test_rescaling_is_idempotent():
    """A second pass must be a no-op, not a second shrink."""
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    once = rescale_tcx(data)
    twice = rescale_tcx(once.data)

    assert twice.factor == pytest.approx(1.0)
    assert twice.data == once.data


def test_explicit_target_moves_the_lap_total_to_match():
    """With an overriding target the file must still come out self-consistent."""
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    result = rescale_tcx(data, 800.0)

    assert texts(result.data, "DistanceMeters") == ["800", "0", "400", "800"]
    assert rescale_tcx(result.data).factor == pytest.approx(1.0)


def test_multiple_laps_split_the_target_in_proportion():
    data = builders.document(
        builders.activity(distances=(0.0, 400.0), lap_distance_m=300.0),
        builders.activity(distances=(0.0, 600.0), lap_distance_m=700.0, start_offset=3600),
    )

    result = rescale_tcx(data, 500.0, tolerance=0.6)

    # Laps keep their 300:700 shape while summing to the new target.
    assert texts(result.data, "DistanceMeters")[0] == "150"
    assert result.target_m == pytest.approx(500.0)


def test_activity_without_a_lap_total_still_scales_its_stream():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=None)

    result = rescale_tcx(data, 900.0)

    assert distances(result.data) == pytest.approx([0.0, 450.0, 900.0])
