#!/usr/bin/env python3
"""Turn a real export into something committable.

A raw TCX is a precise record of where someone lives and when they leave the
house, so `training-data/` is gitignored. But `tests/fixtures/` needs real
Fitbit output — proving the parser copes with what the device actually emits is
a different job from the synthetic builders, which only prove the guards fire.

This bridges the two. It shifts every coordinate by a constant offset, rebases
timestamps to an epoch, strips the device identifiers, and optionally thins the
track so the result is small enough to commit.

Distances, altitudes and heart rates are left exactly as they were: they are what
the fixtures exist to exercise, and they say nothing about where you were.

    python scripts/anonymise.py training-data/123.tcx tests/fixtures/run.tcx
"""

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

# A whole number of degrees keeps the arithmetic exact in the printed decimals,
# so the shifted track has the same shape and the same segment lengths.
LAT_SHIFT = -17.0
LON_SHIFT = 42.0
EPOCH = dt.datetime(2024, 1, 1, 9, 0, 0, tzinfo=dt.UTC)

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_LAT = re.compile(r"(<LatitudeDegrees>)(-?\d+\.?\d*)(</LatitudeDegrees>)")
_LON = re.compile(r"(<LongitudeDegrees>)(-?\d+\.?\d*)(</LongitudeDegrees>)")
_UNIT_ID = re.compile(r"(<UnitId>)[^<]*(</UnitId>)")
_PRODUCT_ID = re.compile(r"(<ProductID>)[^<]*(</ProductID>)")
_TRACKPOINT = re.compile(r"<Trackpoint>.*?</Trackpoint>", re.DOTALL)


def shift_coordinates(text: str) -> str:
    """Move the whole track by a constant offset, preserving its shape."""

    def move(pattern: re.Match[str], delta: float) -> str:
        value = float(pattern[2]) + delta
        return f"{pattern[1]}{value:.7f}{pattern[3]}"

    text = _LAT.sub(lambda m: move(m, LAT_SHIFT), text)
    return _LON.sub(lambda m: move(m, LON_SHIFT), text)


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


def anonymise(text: str, keep_every: int = 1) -> str:
    return thin(strip_identifiers(rebase_timestamps(shift_coordinates(text))), keep_every)


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
    args.destination.write_text(anonymise(text, args.keep_every), encoding="utf-8")
    print(f"{args.source} -> {args.destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
