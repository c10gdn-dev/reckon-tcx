"""Real Fitbit output, anonymised and committed.

These are the counterpart to `builders.py`. The builders construct inputs
designed to trip each guard; these are four genuine exports, so they prove the
parser copes with what the device actually emits — element order, namespace
declarations, the `Creator` block, decimal formatting, positionless trackpoints
inside a GPS activity. None of that is reproduced by a builder, and all of it has
broken something at least once.

`scripts/anonymise.py` produced them: coordinates shifted by a constant offset,
timestamps rebased to a fixed epoch, device identifiers zeroed, and the track
thinned to keep them small. Distances, altitudes and heart rates are untouched,
which is why the factors below match the originals exactly.

Unlike `test_corpus.py` these are committed, so they run everywhere.
"""

import re
from pathlib import Path

import pytest

from reckon.core import tcx
from reckon.core.analyse import analyse_tcx
from reckon.core.rescale import SkipReason, rescale_tcx

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> bytes:
    return (FIXTURES / f"{name}.tcx").read_bytes()


ALL = ["cycle-clean", "indoor-no-gps", "walk-partial-gps", "walk-urban-canyon"]


@pytest.fixture(params=ALL)
def fixture(request) -> bytes:
    return load(request.param)


def test_every_fixture_exists():
    assert sorted(p.stem for p in FIXTURES.glob("*.tcx")) == sorted(ALL)


def test_every_fixture_parses(fixture):
    assert tcx.parse(fixture).tag == tcx.ROOT


def test_timestamps_survive(fixture):
    before = tcx.timestamps(tcx.parse(fixture))

    assert tcx.timestamps(tcx.parse(rescale_tcx(fixture).data)) == before


def test_rescaling_is_idempotent(fixture):
    once = rescale_tcx(fixture)

    assert rescale_tcx(once.data).data == once.data


def test_a_skipped_fixture_is_returned_byte_identical(fixture):
    result = rescale_tcx(fixture)

    if not result.modified:
        assert result.data == fixture


# --- each fixture is here for a specific reason ------------------------------


def test_a_clean_ride_needs_only_a_small_correction():
    result = rescale_tcx(load("cycle-clean"))

    assert result.modified is True
    assert result.factor == pytest.approx(0.9943, abs=1e-4)


def test_an_urban_canyon_walk_needs_a_large_one():
    """Heavy multipath. This is what a symmetric tolerance used to refuse."""
    result = rescale_tcx(load("walk-urban-canyon"))

    assert result.modified is True
    assert result.factor == pytest.approx(0.7229, abs=1e-4)


def test_an_indoor_activity_is_passed_through():
    result = rescale_tcx(load("indoor-no-gps"))

    assert result.modified is False
    assert [s.reason for s in result.skips] == [SkipReason.NO_GPS]


def test_a_partial_track_is_refused():
    result = rescale_tcx(load("walk-partial-gps"))

    assert result.modified is False
    assert [s.reason for s in result.skips] == [SkipReason.PARTIAL_GPS]


def test_coverage_separates_the_partial_track_from_the_rest():
    partial = analyse_tcx(load("walk-partial-gps")).gps_coverage
    complete = [
        analyse_tcx(load(name)).gps_coverage for name in ("cycle-clean", "walk-urban-canyon")
    ]

    assert partial < min(complete)


# --- the anonymiser did its job ----------------------------------------------


@pytest.mark.parametrize("year", [b"2025-", b"2026-"])
def test_fixtures_carry_no_real_dates(fixture, year):
    assert year not in fixture


def test_fixtures_carry_no_device_identifiers(fixture):
    root = tcx.parse(fixture)
    for tag in ("UnitId", "ProductID"):
        for element in root.iter(tcx.qn(tcx.TCX_NS, tag)):
            assert element.text == "0"


# Coordinates were the one thing the anonymiser was never checked on, which is
# why a constant shift declared in a committed script survived here for weeks:
# reversing it returned real coordinates to seven decimal places, in a public
# repository. The dates and device identifiers were asserted from the start and
# were fine. This is the assertion that was missing.
FAKE_BOX_DEGREES = 1.0


def _anonymiser():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "anonymise.py"
    spec = importlib.util.spec_from_file_location("anonymise", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.anonymise


def test_fixtures_carry_no_real_coordinates(fixture):
    """Every coordinate must sit within a degree of Null Island.

    A positive assertion about where the points *are*, not a negative one about
    where they are not — there is no list of real locations to exclude, and a
    test that tried would pass for every location it had not thought of.

    `scripts/anonymise.py` generates these rather than transforming the
    originals, so nothing here is a function of a real coordinate. A regression
    to any shift-based scheme fails this immediately, wherever it shifts to.
    """
    text = fixture.decode()
    values = [
        float(value)
        for pattern in (r"<LatitudeDegrees>([-\d.]+)", r"<LongitudeDegrees>([-\d.]+)")
        for value in re.findall(pattern, text)
    ]

    assert all(abs(value) < FAKE_BOX_DEGREES for value in values), (
        "a coordinate sits outside the fake origin box; anonymise.py must "
        "replace coordinates, never transform them"
    )


def test_the_anonymiser_output_does_not_depend_on_the_input_coordinates():
    """The property that makes it irreversible, asserted rather than assumed.

    Two documents differing only in their coordinates must anonymise to the same
    bytes. Any shift — constant, random, per-file — fails this, because a shift
    carries the original through into the output and only hides it behind a key.
    """
    # `scripts/` is not a package and is not on the path; loaded by location so
    # this test binds to the script the repository actually ships.
    anonymise = _anonymiser()

    one = load("cycle-clean").decode()
    other = re.sub(
        r"<LatitudeDegrees>[-\d.]+</LatitudeDegrees>",
        "<LatitudeDegrees>55.8549433</LatitudeDegrees>",
        one,
    )

    assert anonymise(one, seed="x") == anonymise(other, seed="x")
