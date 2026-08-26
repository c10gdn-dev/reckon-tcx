"""Command line entry point.

Phase 2 ships one subcommand: `rescale`, which is deliberately the one that
works with no credentials, no network and no config.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import IO, Any

from reckon import __version__
from reckon.core.errors import ReckonError
from reckon.core.rescale import DEFAULT_TOLERANCE, RescaleResult, ToleranceAction, rescale_tcx

_DISTANCE_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>km|mi)?", re.IGNORECASE)
_UNITS_IN_METRES = {"": 1.0, "km": 1000.0, "mi": 1609.344}
_DISTANCE_HELP = "expected a distance like 10.2km, 6.3mi, or 10200 (bare number means metres)"


def parse_distance(text: str) -> float:
    """Parse `10.2km`, `6.3mi` or a bare number of metres into metres."""
    match = _DISTANCE_PATTERN.fullmatch(text)
    if match is None:
        raise argparse.ArgumentTypeError(f"{text!r}: {_DISTANCE_HELP}")
    metres = float(match["value"]) * _UNITS_IN_METRES[(match["unit"] or "").lower()]
    if metres <= 0:
        raise argparse.ArgumentTypeError(f"{text!r}: distance must be greater than zero")
    return metres


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reckon",
        description="Corrects Fitbit's GPS distance inflation before uploading to Strava.",
    )
    parser.add_argument("--version", action="version", version=f"reckon {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    rescale = subcommands.add_parser(
        "rescale",
        help="rescale a TCX file's distance stream to its own Fitbit total (offline)",
        description=(
            "Rescale a TCX file's cumulative distance stream so its total matches "
            "Fitbit's own figure, which the file already carries in "
            "Lap/DistanceMeters. Timestamps and coordinates are never modified. "
            "Files with nothing to scale, such as an indoor activity with no GPS, "
            "are passed through byte-identically."
        ),
    )
    rescale.add_argument("input", type=Path, help="TCX file to read")
    rescale.add_argument(
        "--distance",
        type=parse_distance,
        metavar="DIST",
        help=("the true total, overriding the file's own Lap/DistanceMeters; " + _DISTANCE_HELP),
    )
    rescale.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write here instead of stdout",
    )
    rescale.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"how far the factor may sit from 1 (default {DEFAULT_TOLERANCE})",
    )
    rescale.add_argument(
        "--on-tolerance",
        type=ToleranceAction,
        choices=list(ToleranceAction),
        default=ToleranceAction.ABORT,
        help="what to do when the factor is outside tolerance (default abort)",
    )
    rescale.set_defaults(handler=_rescale_command)
    return parser


def main(argv: list[str] | None = None, *, stdout: Any = None, stderr: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    return args.handler(args, out, err)


def _rescale_command(args: argparse.Namespace, out: Any, err: Any) -> int:
    try:
        data = args.input.read_bytes()
    except OSError as exc:
        print(f"reckon: cannot read {args.input}: {exc.strerror}", file=err)
        return 1

    try:
        result = rescale_tcx(
            data,
            args.distance,
            tolerance=args.tolerance,
            on_tolerance=args.on_tolerance,
        )
    except ReckonError as exc:
        print(f"reckon: {exc}", file=err)
        return 1

    for warning in result.warnings:
        print(f"reckon: warning: {warning}", file=err)
    _report(result, args, err)

    if args.output is None:
        _binary(out).write(result.data)
    else:
        args.output.write_bytes(result.data)
    return 0


def _report(result: RescaleResult, args: argparse.Namespace, err: Any) -> None:
    if not result.modified:
        print(
            f"reckon: {result.trackpoint_count} trackpoints, nothing to rescale, "
            f"file written unchanged",
            file=err,
        )
        return
    source = "given" if args.distance is not None else "from file"
    print(
        f"reckon: {result.trackpoint_count} trackpoints  "
        f"gps {result.gps_total_m:.1f} m  "
        f"target {result.target_m:.1f} m ({source})  "
        f"result {result.result_total_m:.1f} m  "
        f"factor {result.factor:.6f}",
        file=err,
    )


def _binary(stream: Any) -> IO[bytes]:
    """The byte-accepting half of a text stream, or the stream itself if it is already binary."""
    return getattr(stream, "buffer", stream)


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    sys.exit(main())
