#!/usr/bin/env python3
"""Compare a local TCX against the API's export of the same activity.

Argparse plumbing over `reckon.core.tcx`. Written after the API's export turned
out to omit heart rate entirely — a difference that hid for days because the
corpus holds files from both routes and records which nowhere.

    python scripts/compare_export.py training-data/6436069663605072632.tcx

Use it whenever a corpus finding might be explained by where a file came from
rather than by what it measures. That confusion has already produced one wrong
conclusion.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from reckon.cli import _build_pipeline
from reckon.core import tcx
from reckon.stores.file import DEFAULT_PATH


def census(data: bytes) -> tuple[int, Counter, bool]:
    activity = next(tcx.activities(tcx.parse(data)))
    points = list(tcx.trackpoints(activity))
    tags = Counter(e.tag.rpartition("}")[2] for p in points for e in p)
    return len(points), tags, tcx.has_position(activity)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("file", type=Path, help="a local TCX; its stem is the activity id")
    parser.add_argument("--id", help="override the activity id")
    parser.add_argument("--store", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    from argparse import Namespace

    local = args.file.read_bytes()
    activity_id = args.id or args.file.stem
    remote = _build_pipeline(Namespace(store=args.store, transport=None)).fetch(
        activity_id, raw=True
    )

    rows = [("local file", local), ("API export", remote)]
    print(f"activity {activity_id}\n")
    print(f"  {'source':<12}{'bytes':>9}{'points':>8}{'gps':>6}  elements")
    for label, data in rows:
        count, tags, gps = census(data)
        print(f"  {label:<12}{len(data):>9}{count:>8}{gps!s:>6}  {dict(tags)}")

    _, local_tags, _ = census(local)
    _, remote_tags, _ = census(remote)
    missing = sorted(set(local_tags) - set(remote_tags))
    extra = sorted(set(remote_tags) - set(local_tags))
    print()
    if missing:
        print(f"  the API export is MISSING: {', '.join(missing)}")
    if extra:
        print(f"  the API export adds:       {', '.join(extra)}")
    if not missing and not extra:
        print("  both carry the same element types")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
