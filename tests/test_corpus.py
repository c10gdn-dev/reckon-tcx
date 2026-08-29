"""The transform against real exports.

`tests/builders.py` proves the guards fire on inputs constructed to trip them.
This file does the opposite job: it runs over whatever real Fitbit exports are
present and asserts that nothing surprising happens to any of them.

`training-data/` is gitignored, so this whole module skips on a fresh clone and
in CI. That is deliberate — a forker must be able to run a green suite without
owning a Fitbit.
"""

import os
from pathlib import Path

import pytest

from reckon.core import tcx
from reckon.core.analyse import analyse_tcx
from reckon.core.rescale import SkipReason, rescale_tcx

# Overridable so the skip path itself can be exercised, and so a corpus kept
# outside the repository can still be run against.
CORPUS = Path(
    os.environ.get("RECKON_CORPUS") or Path(__file__).resolve().parent.parent / "training-data"
)
FILES = sorted(CORPUS.glob("*.tcx")) if CORPUS.is_dir() else []

# Widest factor considered plausible for a real activity. Not the transform's own
# tolerance — this is the test asserting the *corpus* stays in the range the
# calibration was done over, so a wildly different future export is noticed.
PLAUSIBLE = (0.6, 1.0)

pytestmark = pytest.mark.skipif(not FILES, reason="training-data/ is empty")


@pytest.fixture(scope="module", params=FILES, ids=[p.stem[:8] for p in FILES])
def export(request) -> bytes:
    return request.param.read_bytes()


def test_every_export_parses(export):
    assert tcx.parse(export).tag == tcx.ROOT


def test_the_transform_completes_without_raising(export):
    rescale_tcx(export)


def test_timestamps_are_never_modified(export):
    before = tcx.timestamps(tcx.parse(export))

    after = tcx.timestamps(tcx.parse(rescale_tcx(export).data))

    assert after == before


def test_geometry_is_never_modified(export):
    before, after = tcx.parse(export), tcx.parse(rescale_tcx(export).data)

    for tag in ("LatitudeDegrees", "LongitudeDegrees", "AltitudeMeters"):
        name = tcx.qn(tcx.TCX_NS, tag)
        assert [e.text for e in after.iter(name)] == [e.text for e in before.iter(name)], tag


def test_heart_rate_is_never_modified(export):
    before, after = tcx.parse(export), tcx.parse(rescale_tcx(export).data)
    name = tcx.qn(tcx.TCX_NS, "Value")

    assert [e.text for e in after.iter(name)] == [e.text for e in before.iter(name)]


def test_the_factor_is_plausible_or_the_file_is_skipped(export):
    result = rescale_tcx(export)

    if result.modified:
        assert PLAUSIBLE[0] <= result.factor <= PLAUSIBLE[1]
    else:
        assert result.skips, "an unmodified file must say why"


def test_a_skipped_file_comes_back_byte_identical(export):
    result = rescale_tcx(export)

    if not result.modified:
        assert result.data == export


def test_a_corrected_file_hits_its_target_exactly(export):
    result = rescale_tcx(export)

    if result.modified:
        root = tcx.parse(result.data)
        stream = [
            tcx.read_float(e)
            for p in root.iter(tcx.TRACKPOINT)
            for e in p.findall(tcx.DISTANCE_METERS)
        ]
        assert stream[-1] == pytest.approx(result.target_m, abs=0.01)


def test_the_activity_total_survives_the_correction(export):
    """Lap/DistanceMeters is the target, so it must come out untouched."""
    before = tcx.lap_distance_total(next(tcx.activities(tcx.parse(export))))
    after = tcx.lap_distance_total(next(tcx.activities(tcx.parse(rescale_tcx(export).data))))

    if before is not None:
        assert after == pytest.approx(before, abs=0.01)


def test_rescaling_twice_changes_nothing(export):
    once = rescale_tcx(export)

    assert rescale_tcx(once.data).data == once.data


def test_the_distance_stream_stays_monotonic(export):
    root = tcx.parse(rescale_tcx(export).data)
    stream = [
        tcx.read_float(e) for p in root.iter(tcx.TRACKPOINT) for e in p.findall(tcx.DISTANCE_METERS)
    ]

    assert all(stream[i] >= stream[i - 1] for i in range(1, len(stream)))


def test_analysis_agrees_with_the_transform(export):
    stats = analyse_tcx(export)
    result = rescale_tcx(export, tolerance=1.0)

    assert stats.corrected == result.modified
    if stats.factor is not None:
        assert stats.factor == pytest.approx(result.factor)


def test_partial_gps_is_the_only_reason_a_gps_file_is_skipped(export):
    """A file with GPS should be corrected unless its track is incomplete."""
    result = rescale_tcx(export)
    reasons = {s.reason for s in result.skips}

    if not result.modified and SkipReason.NO_GPS not in reasons:
        assert reasons == {SkipReason.PARTIAL_GPS}


def test_moving_time_moves_by_seconds_not_minutes(export):
    """The README promises this; the corpus is where it gets checked."""
    assert abs(analyse_tcx(export).moving_delta_s) < 120
