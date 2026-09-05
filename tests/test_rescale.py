"""The transform itself: the factor, the guards, and what must not move.

The invariant that matters most here is temporal. `test_timestamps_survive_*`
compares `tcx.timestamps()` before and after; every other assertion in this file
is about arithmetic, and arithmetic is recoverable. A shifted timestamp is not.
"""

import pytest

import builders
from reckon.core import tcx
from reckon.core.errors import MalformedTCX, MissingTarget, ToleranceExceeded
from reckon.core.rescale import (
    MAX_CREDIBLE_FACTOR,
    SkipReason,
    ToleranceAction,
    rescale_tcx,
)


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
    assert any("no activity carries a usable GPS distance stream" in w for w in result.warnings)


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


def test_factor_far_below_one_aborts_by_default():
    with pytest.raises(ToleranceExceeded) as caught:
        rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 200.0)

    error = caught.value
    assert error.factor == pytest.approx(0.2)
    assert error.gps_total_m == pytest.approx(1000.0)
    assert error.target_m == pytest.approx(200.0)
    assert error.tolerance == pytest.approx(0.4)
    assert "--on-tolerance clamp|proceed" in str(error)


def test_factor_below_tolerance_can_be_clamped():
    result = rescale_tcx(
        builders.tcx(distances=(0.0, 500.0, 1000.0)),
        200.0,
        on_tolerance=ToleranceAction.CLAMP,
    )

    assert result.factor == pytest.approx(0.6)
    assert result.result_total_m == pytest.approx(600.0)
    assert any("clamped to 0.6000" in w for w in result.warnings)


def test_factor_above_tolerance_can_be_clamped():
    result = rescale_tcx(
        builders.tcx(distances=(0.0, 500.0, 1000.0)),
        5000.0,
        on_tolerance=ToleranceAction.CLAMP,
    )

    assert result.factor == pytest.approx(1.4)


def test_factor_outside_tolerance_can_proceed_unclamped():
    result = rescale_tcx(
        builders.tcx(distances=(0.0, 500.0, 1000.0)),
        200.0,
        on_tolerance=ToleranceAction.PROCEED,
    )

    assert result.factor == pytest.approx(0.2)
    assert result.result_total_m == pytest.approx(200.0)
    assert any("outside tolerance" in w and "proceeding" in w for w in result.warnings)


def test_factor_exactly_on_the_tolerance_bound_is_allowed():
    result = rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 800.0, tolerance=0.2)

    assert result.factor == pytest.approx(0.8)


# --- the guard is asymmetric ------------------------------------------------


def test_heavy_jitter_from_the_file_is_corrected_not_refused():
    """A real walk measured 0.723. A symmetric 0.2 band refused it; it must not."""
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=723.0)

    result = rescale_tcx(data)

    assert result.modified is True
    assert result.factor == pytest.approx(0.723)


def test_the_low_bound_still_catches_an_order_of_magnitude_error():
    """Metres supplied where kilometres were meant, say."""
    with pytest.raises(ToleranceExceeded):
        rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 10.0)


def test_an_explicit_target_is_bounded_above_as_well():
    """A caller's number can be wrong in either direction."""
    with pytest.raises(ToleranceExceeded):
        rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0)), 3000.0)


def test_an_absurd_target_on_a_complete_track_is_a_tolerance_breach():
    """Three times the recorded distance, with every fix present.

    Once a factor above 1 needs corroboration, a complete track measuring
    *wildly* short is no longer partial GPS — nothing about the track suggests a
    missing route. It is a target that cannot be right, which is exactly what the
    tolerance guard is for, and its message says to check the distance.
    """
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=3000.0)

    with pytest.raises(ToleranceExceeded, match="check the target distance"):
        rescale_tcx(data)


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


# --- partial GPS: the case where rescaling would fabricate -------------------


def partial_track(locked: int = 2, unlocked: int = 8, lap_distance_m: float = 900.0) -> bytes:
    """An activity that only got a fix for the last `locked` trackpoints."""
    distances = (None,) * unlocked + tuple(float(i * 100) for i in range(locked))
    return builders.tcx(
        distances=distances,
        positions=[False] * unlocked + [True] * locked,
        lap_distance_m=lap_distance_m,
    )


def test_partial_gps_is_refused_rather_than_scaled():
    result = rescale_tcx(partial_track())

    assert result.modified is False
    assert [s.reason for s in result.skips] == [SkipReason.PARTIAL_GPS]
    assert "GPS covers only" in result.skips[0].detail
    assert "the distance stream is incomplete" in result.skips[0].detail


def test_partial_gps_returns_the_original_bytes_untouched():
    data = partial_track()

    assert rescale_tcx(data).data == data


def test_partial_gps_threshold_is_adjustable():
    """The default sits in a gap no real file has been observed in, so it moves.

    The lap total here matches the stream, so the factor rule stays quiet and
    coverage is the only thing deciding.
    """
    data = partial_track(lap_distance_m=100.0)

    assert rescale_tcx(data).modified is False
    assert rescale_tcx(data, min_gps_coverage=0.0).modified is True


def test_a_complete_track_is_not_mistaken_for_a_partial_one():
    result = rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0))

    assert result.modified is True
    assert result.skips == ()


def test_a_short_dropout_is_caught_by_the_factor_even_at_high_coverage():
    """A dropout too brief for the coverage threshold, caught by the factor.

    Coverage passes at 90%, so the factor is what notices — but only because a
    fix is missing, which is where the route could have gone. A complete track
    measuring short is a different thing entirely, and is corrected.
    """
    data = builders.tcx(
        distances=(0.0, None, *(float(i * 10) for i in range(1, 9))),
        positions=[True, False] + [True] * 8,
        lap_distance_m=500.0,
    )

    result = rescale_tcx(data)

    assert result.modified is False
    assert {s.reason for s in result.skips} == {SkipReason.PARTIAL_GPS}
    assert "not fully recorded" in result.skips[0].detail


def test_an_explicit_target_above_the_stream_is_a_tolerance_matter_not_a_dropout():
    """A bad --distance is the caller's error, not evidence of a missing track."""
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    with pytest.raises(ToleranceExceeded):
        rescale_tcx(data, 5000.0)


def test_an_explicit_target_slightly_above_the_stream_still_scales():
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)

    result = rescale_tcx(data, 1100.0)

    assert result.modified is True
    assert result.factor == pytest.approx(1.1)


def test_a_factor_a_hair_above_one_from_the_file_is_tolerated_as_rounding():
    """MAX_CREDIBLE_FACTOR absorbs rounding in the two recorded totals."""
    data = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=1000.0)

    result = rescale_tcx(data)

    assert result.factor == pytest.approx(1.0)
    assert MAX_CREDIBLE_FACTOR > 1.0


def test_no_gps_and_no_stream_carry_their_own_skip_reasons():
    no_gps = rescale_tcx(builders.tcx(with_position=False))
    no_stream = rescale_tcx(builders.tcx(distances=(None, None, None)))

    assert [s.reason for s in no_gps.skips] == [SkipReason.NO_GPS]
    assert [s.reason for s in no_stream.skips] == [SkipReason.NO_DISTANCE_STREAM]


def test_skips_name_the_activity_they_refer_to():
    data = builders.document(
        builders.activity(distances=(0.0, 500.0), activity_id="first", lap_distance_m=480.0),
        builders.activity(
            distances=(None, None), activity_id="second", with_position=False, start_offset=3600
        ),
    )

    result = rescale_tcx(data)

    assert [(s.activity, s.reason) for s in result.skips] == [("second", SkipReason.NO_GPS)]
    assert result.modified is True


# --- the recording-gap warning ----------------------------------------------
#
# Added after a real interval session reported 100% GPS coverage while 47% of its
# elapsed time fell between trackpoints. It warns and never refuses: the stream
# chords across a gap, so distance survives and only the route's shape degrades.


def gappy(gap_seconds: int) -> bytes:
    """A track sampled every second, interrupted once."""
    offsets = [0, 1, 2, 3, 4, 5, 5 + gap_seconds, 6 + gap_seconds]
    points = [
        builders.trackpoint(offset_seconds=o, distance_m=float(i * 20))
        for i, o in enumerate(offsets)
    ]
    return builders.document(
        '<Activity Sport="Running"><Id>gappy</Id>'
        + builders.lap(trackpoints=points, distance_m=130.0)
        + "</Activity>"
    )


def test_a_large_recording_gap_warns() -> None:
    result = rescale_tcx(gappy(120))
    assert any("no trackpoint" in w for w in result.warnings)
    assert any("longest 120s" in w for w in result.warnings)


def test_the_gap_warning_does_not_refuse_the_file() -> None:
    """Distance survives a gap; only shape is lost. Refusing would be wrong."""
    result = rescale_tcx(gappy(120))
    assert result.modified is True
    assert result.skips == ()


def test_an_uninterrupted_track_does_not_warn_about_gaps() -> None:
    document = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=930.0)
    assert not any("no trackpoint" in w for w in rescale_tcx(document).warnings)


def test_the_gap_threshold_is_configurable() -> None:
    assert not any(
        "no trackpoint" in w for w in rescale_tcx(gappy(120), max_gap_fraction=1.0).warnings
    )


def test_the_warning_names_the_share_and_the_worst_gap() -> None:
    """Both, because "many small" and "one long" are different problems."""
    (warning,) = [w for w in rescale_tcx(gappy(60)).warnings if "no trackpoint" in w]
    assert "%" in warning
    assert "1 gaps" in warning
    assert "longest 60s" in warning


# --- a factor above 1 needs corroboration -----------------------------------
#
# The original rule refused any file whose own total exceeded the GPS sum, on the
# reasoning that jitter is additive so a complete track can only measure long.
# That is half the mechanism: the stream sums straight lines between fixes, so it
# under-measures every curve too. A real 14 km run measured 0.57% short with a
# perfect track, and was refused for it.


def short_measuring(positions=None) -> bytes:
    """Ten fixes whose stream totals 1% less than the lap says it should.

    1% is past MAX_CREDIBLE_FACTOR and well inside the tolerance band, which is
    the range the corroboration rule governs.
    """
    return builders.tcx(
        distances=tuple(float(i * 100) for i in range(10)),
        lap_distance_m=909.0,
        positions=positions,
        with_heart_rate=False,
    )


def test_a_complete_track_measuring_short_is_corrected() -> None:
    """Chords are shorter than arcs. That is not a missing route."""
    result = rescale_tcx(short_measuring())
    assert result.modified is True
    assert result.skips == ()
    assert result.result_total_m == pytest.approx(909.0)


def test_a_track_with_an_unlocked_stretch_measuring_short_is_still_refused() -> None:
    """Where the route *could* have gone missing, a short measurement says it did."""
    result = rescale_tcx(short_measuring(positions=[True] * 4 + [False] + [True] * 5))
    assert result.modified is False
    assert [str(s.reason) for s in result.skips] == ["partial_gps"]
    assert "not fully recorded" in result.skips[0].detail


def test_the_refusal_explains_both_halves() -> None:
    result = rescale_tcx(short_measuring(positions=[True] * 4 + [False] + [True] * 5))
    detail = result.skips[0].detail
    assert "exceeds the GPS distance" in detail
    assert "not fully recorded" in detail


def test_a_track_with_a_large_recording_gap_measuring_short_is_refused() -> None:
    """Coverage cannot see a gap where no trackpoint was written; this can."""
    points = [
        builders.trackpoint(offset_seconds=o, distance_m=d, with_heart_rate=False)
        for o, d in ((0, 0.0), (1, 300.0), (2, 600.0), (600, 1000.0))
    ]
    document = builders.document(
        '<Activity Sport="Running"><Id>gappy</Id>'
        + builders.lap(trackpoints=points, distance_m=1100.0)
        + "</Activity>"
    )
    result = rescale_tcx(document)
    assert result.modified is False
    assert [str(s.reason) for s in result.skips] == ["partial_gps"]


def test_a_factor_below_one_is_unaffected_by_any_of_this() -> None:
    """The ordinary case: the stream over-measured, which is what jitter does."""
    result = rescale_tcx(builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=800.0))
    assert result.modified is True
    assert result.factor == pytest.approx(0.8)
