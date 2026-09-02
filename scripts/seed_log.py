#!/usr/bin/env python3
"""Mark existing activities as already handled, so a first sync does not re-upload.

Argparse plumbing only; `Pipeline.mark_done` does the work and is tested there.

Run this once when adopting Reckon on an account whose activities already reach
Strava some other way — the built-in Google Health or Fitbit connection, say.
Without it the first `reckon sync` uploads a history that is already there, and
Strava's deduplication will not save you: it matches on `external_id`, and
whatever put those activities there first used its own.

    python scripts/seed_log.py --since 2025-01-01 --dry-run
    python scripts/seed_log.py --since 2025-01-01

Nothing is fetched and nothing is uploaded. It lists the window and writes one
record per activity.
"""

import argparse
import datetime as dt
import sys
from argparse import Namespace
from pathlib import Path

from reckon.cli import _build_pipeline, _timestamp
from reckon.pipeline import summarise
from reckon.stores.file import DEFAULT_PATH

REASON = "already on Strava before Reckon; not re-uploaded"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", type=_timestamp, required=True, metavar="DATE")
    parser.add_argument("--until", type=_timestamp, metavar="DATE", help="default: now")
    parser.add_argument("--reason", default=REASON)
    parser.add_argument("--store", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--dry-run", action="store_true", help="list without recording")
    args = parser.parse_args(argv)

    until = args.until or dt.datetime.now(tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pipeline = _build_pipeline(Namespace(store=args.store, transport=None))

    if args.dry_run:
        found = list(pipeline.exercises(args.since, until))
        for exercise in found:
            print(f"  would mark {exercise.id:<22}{exercise.display_name}")
        print(f"\n{len(found)} activities between {args.since} and {until}")
        return 0

    outcomes = pipeline.mark_done(start_time=args.since, end_time=until, reason=args.reason)
    for outcome in outcomes:
        marker = "=" if not outcome.fresh else " "
        print(f"{marker} {outcome.activity_id:<22}{outcome.name[:18]:<19}{outcome.reason}")
    print("\n" + "  ".join(f"{n} {label}" for label, n in sorted(summarise(outcomes).items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
