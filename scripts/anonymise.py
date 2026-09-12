#!/usr/bin/env python3
"""Turn a real export into something committable.

A raw TCX is a precise record of where someone lives and when they leave the
house, so `training-data/` is gitignored. But `tests/fixtures/` needs real
Fitbit output — proving the parser copes with what the device actually emits is
a different job from the synthetic builders, which only prove the guards fire.

This bridges the two. It **replaces** every coordinate with a generated one,
rebases timestamps to an epoch, strips the device identifiers, and optionally
thins the track so the result is small enough to commit.

Distances, altitudes and heart rates are left exactly as they were: they are what
the fixtures exist to exercise, and they say nothing about where you were.

**Replaced, not transformed — and that distinction is the whole point.** Until
2026-09-12 this shifted every coordinate by a constant, `LAT_SHIFT = -17.0` and
`LON_SHIFT = 42.0`, declared in this file, committed to the same public
repository as the fixtures it produced. Reversing it was two lines. A shift is a
*function of the original*, so the original survives inside the output and the
only question is whether the attacker has the key; publishing the script
published the key. Randomising the constant would not have fixed it either,
because the route's shape and segment lengths were preserved on purpose, and a
shape is matchable against a map without any key at all.

Generated coordinates have nothing to reverse. The output does not depend on the
input coordinates in any way — they are read only to know how many to emit.

    python scripts/anonymise.py training-data/123.tcx tests/fixtures/run.tcx
"""

import argparse
import datetime as dt
import random
import re
import sys
from pathlib import Path

# Null Island: 0°N 0°E, in the Gulf of Guinea. Chosen because it is unmistakably
# not where anybody went — a fixture that looked like a plausible town would
# invite the question this script exists to foreclose.
FAKE_ORIGIN = (0.0, 0.0)

# Roughly 1 m and 8 m at the equator. A random walk on this scale produces a
# track whose point spacing is in the right order for a walk or a ride, so
# `analyse_tcx` returns finite, unremarkable numbers rather than absurd ones.
# Nothing asserts on them; this only keeps a reader from being distracted.
STEP_DEGREES = 1e-5
JITTER_DEGREES = 8e-5

EPOCH = dt.datetime(2024, 1, 1, 9, 0, 0, tzinfo=dt.UTC)

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_LAT = re.compile(r"(<LatitudeDegrees>)(-?\d+\.?\d*)(</LatitudeDegrees>)")
_LON = re.compile(r"(<LongitudeDegrees>)(-?\d+\.?\d*)(</LongitudeDegrees>)")
_UNIT_ID = re.compile(r"(<UnitId>)[^<]*(</UnitId>)")
_PRODUCT_ID = re.compile(r"(<ProductID>)[^<]*(</ProductID>)")
_TRACKPOINT = re.compile(r"<Trackpoint>.*?</Trackpoint>", re.DOTALL)


def replace_coordinates(text: str, seed: str) -> str:
    """Throw every coordinate away and emit a generated walk in its place.

    A random walk rather than independent noise, so the result still reads as a
    track: consecutive points are metres apart and the parser, the coverage
    measure and the analyser all see the shape of thing they see in real data.

    The walk is seeded from the *output filename*, so re-running on the same
    target reproduces the same fixture and a regeneration shows an empty diff.
    Seeding from anything derived from the input would put the input back into
    the output, which is the mistake this replaced.

    What is preserved is only the *count* and the *position in the document* —
    a trackpoint that had a fix still has one, and one that did not still does
    not, which is what `gps_coverage` is measuring in these fixtures.
    """
    rng = random.Random(seed)
    latitude, longitude = FAKE_ORIGIN

    def walk() -> tuple[float, float]:
        nonlocal latitude, longitude
        latitude += rng.uniform(-JITTER_DEGREES, JITTER_DEGREES) + STEP_DEGREES
        longitude += rng.uniform(-JITTER_DEGREES, JITTER_DEGREES) + STEP_DEGREES
        return latitude, longitude

    # Latitude and longitude are emitted as a pair per trackpoint, so the two
    # substitutions have to advance the walk together rather than independently.
    points = iter([walk() for _ in _LAT.findall(text)])
    pending: list[tuple[float, float]] = []

    def take_lat(match: re.Match[str]) -> str:
        point = next(points)
        pending.append(point)
        return f"{match[1]}{point[0]:.7f}{match[3]}"

    def take_lon(match: re.Match[str]) -> str:
        return f"{match[1]}{pending.pop(0)[1]:.7f}{match[3]}"

    text = _LAT.sub(take_lat, text)
    return _LON.sub(take_lon, text)


def rebase_timestamps(text: str) -> str:
    """Rebase every timestamp so the activity starts at a fixed epoch."""
    stamps = _TIMESTAMP.findall(text)
    if not stamps:
        return text
    first = dt.datetime.fromisoformat(stamps[0])

    def rebase(match: re.Match[str]) -> str:
        moment = EPOCH + (dt.datetime.fromisoformat(match[0]) - first)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return _TIMESTAMP.sub(rebase, text)


def strip_identifiers(text: str) -> str:
    """Remove the device serial and product identifiers."""
    text = _UNIT_ID.sub(r"\g<1>0\g<2>", text)
    return _PRODUCT_ID.sub(r"\g<1>0\g<2>", text)


def thin(text: str, keep_every: int) -> str:
    """Keep every nth trackpoint, so a fixture is small enough to commit.

    The final trackpoint is always kept, because the last `DistanceMeters` is the
    activity's stream total and dropping it would change what the fixture means.
    """
    if keep_every <= 1:
        return text
    points = list(_TRACKPOINT.finditer(text))
    if not points:
        return text
    keep = set(range(0, len(points), keep_every)) | {len(points) - 1}
    out, cursor = [], 0
    for index, match in enumerate(points):
        out.append(text[cursor : match.start()])
        if index in keep:
            out.append(match.group(0))
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


def anonymise(text: str, keep_every: int = 1, seed: str = "reckon") -> str:
    """Everything, in the order that matters: coordinates first, thinning last."""
    replaced = replace_coordinates(text, seed)
    return thin(strip_identifiers(rebase_timestamps(replaced)), keep_every)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="real export to anonymise")
    parser.add_argument("destination", type=Path, help="where to write the fixture")
    parser.add_argument(
        "--keep-every",
        type=int,
        default=1,
        metavar="N",
        help="keep every nth trackpoint, to shrink the fixture (default 1, keep all)",
    )
    args = parser.parse_args(argv)

    text = args.source.read_text(encoding="utf-8")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    # Seeded on the destination name so regenerating a fixture is a no-op diff.
    args.destination.write_text(
        anonymise(text, args.keep_every, seed=args.destination.name), encoding="utf-8"
    )
    print(f"{args.source} -> {args.destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
