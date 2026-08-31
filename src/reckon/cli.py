"""Command line entry point.

`rescale` and `analyse` work with no credentials, no network and no config, and
that stays true — they are how the premise is evaluated in thirty seconds.
`fetch` and `sync` need both services authorised; everything they do beyond
argument parsing and configuration belongs to `pipeline.py`.
"""

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path
from typing import IO, Any

from reckon import __version__
from reckon.clients import health as health_api
from reckon.clients import strava as strava_api
from reckon.clients.http import retrying, send
from reckon.core import svg
from reckon.core.analyse import ActivityStats, analyse_tcx, summarise
from reckon.core.errors import ReckonError
from reckon.core.rescale import DEFAULT_TOLERANCE, RescaleResult, ToleranceAction, rescale_tcx
from reckon.pipeline import Outcome, Pipeline, token_holder
from reckon.pipeline import summarise as summarise_outcomes
from reckon.stores.file import DEFAULT_PATH, FileStore

DEFAULT_CORPUS = Path("training-data")
DEFAULT_PLOT = Path("docs/factor-distribution.svg")

# How far back `sync` looks when not told. Long enough that a first run picks up
# a normal week, short enough not to burn the exercise quota 25 at a time.
DEFAULT_SINCE_DAYS = 7

# Client credentials come from the environment rather than a config file: they
# are the same values the AWS side reads from SSM, and a file would be a second
# place to leak them from.
_ENV = {
    "google": ("RECKON_GOOGLE_CLIENT_ID", "RECKON_GOOGLE_CLIENT_SECRET"),
    "strava": ("RECKON_STRAVA_CLIENT_ID", "RECKON_STRAVA_CLIENT_SECRET"),
}

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

    analyse = subcommands.add_parser(
        "analyse",
        help="report the factor distribution across a corpus of TCX files",
        description=(
            "Measure every TCX file in a directory: the correction factor, how "
            "much of the activity GPS actually covered, how noisy the track was, "
            "and how far the derived figures move when the stream is rescaled. "
            "Reads only; nothing is written unless --plot is given."
        ),
    )
    analyse.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"directory of .tcx files to measure (default {DEFAULT_CORPUS})",
    )
    analyse.add_argument(
        "--plot",
        nargs="?",
        type=Path,
        const=DEFAULT_PLOT,
        metavar="SVG",
        help=f"also write a factor histogram here (default {DEFAULT_PLOT})",
    )
    analyse.set_defaults(handler=_analyse_command)

    fetch = subcommands.add_parser(
        "fetch",
        help="download one activity's TCX from Google Health",
        description=(
            "Fetch one activity as TCX and correct it. The target distance is "
            "already in the file, so this needs no second request for it. "
            "Nothing is uploaded and nothing is recorded."
        ),
    )
    fetch.add_argument("activity_id", help="the Google Health data point id")
    fetch.add_argument(
        "--raw", action="store_true", help="write Google's bytes untouched, without rescaling"
    )
    fetch.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    _add_store_argument(fetch)
    fetch.set_defaults(handler=_fetch_command)

    sync = subcommands.add_parser(
        "sync",
        help="correct and upload every new activity in a window",
        description=(
            "Fetch each activity Google Health recorded in the window, correct "
            "what can be corrected, and upload all of it to Strava. Activities "
            "that cannot be corrected are uploaded unchanged rather than "
            "dropped. Anything already recorded is left alone."
        ),
    )
    sync.add_argument(
        "--since", type=_timestamp, metavar="DATE", help=f"default: {DEFAULT_SINCE_DAYS} days ago"
    )
    sync.add_argument("--until", type=_timestamp, metavar="DATE", help="default: now")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything except upload and record; print what would happen",
    )
    _add_store_argument(sync)
    sync.set_defaults(handler=_sync_command)
    return parser


def _add_store_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_PATH,
        metavar="PATH",
        help=f"token and dedupe store (default {DEFAULT_PATH})",
    )


def _timestamp(text: str) -> str:
    """Accept a date or a full timestamp, and return RFC 3339 UTC.

    A bare date means midnight UTC. Google's filter wants RFC 3339 and its
    notifications already speak it, so this is the one place the two meet.
    """
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{text!r}: expected a date like 2026-08-01 or a timestamp like 2026-08-01T09:30:00Z"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return _rfc3339(moment)


def _rfc3339(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(
    argv: list[str] | None = None,
    *,
    stdout: Any = None,
    stderr: Any = None,
    transport: Any = None,
) -> int:
    """Run one command.

    Streams and transport are parameters for the same reason: so a test supplies
    real objects that happen to be boring, rather than patching module globals.
    The default transport is the retrying urllib one — production passes nothing.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    args.transport = retrying(send) if transport is None else transport
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


def _analyse_command(args: argparse.Namespace, out: Any, err: Any) -> int:
    paths = sorted(args.corpus.glob("*.tcx"))
    if not paths:
        print(f"reckon: no .tcx files in {args.corpus}", file=err)
        return 1

    measured: list[tuple[str, ActivityStats]] = []
    for path in paths:
        try:
            measured.append((path.stem, analyse_tcx(path.read_bytes())))
        except (OSError, ReckonError) as exc:
            print(f"reckon: {path.name}: {exc}", file=err)
            return 1

    _print_table(measured, out)
    summary = summarise([s for _, s in measured])
    print("", file=out)
    print(f"{summary.corrected} of {summary.files} corrected", file=out)
    if summary.factor_mean is not None:
        spread = "" if summary.factor_stdev is None else f"  stdev {summary.factor_stdev:.4f}"
        print(
            f"factor  {summary.factor_min:.4f}-{summary.factor_max:.4f}"
            f"  mean {summary.factor_mean:.4f}{spread}",
            file=out,
        )
    print(f"worst moving-time change  {summary.worst_moving_delta_s:.0f}s", file=out)
    for reason, count in summary.skipped:
        print(f"skipped  {reason}  x{count}", file=out)

    if args.plot is not None:
        factors = [s.factor for _, s in measured if s.factor is not None]
        if not factors:
            print("reckon: nothing to plot; no file produced a factor", file=err)
            return 1
        args.plot.parent.mkdir(parents=True, exist_ok=True)
        args.plot.write_bytes(
            svg.histogram(
                factors,
                title=f"Correction factor across {len(factors)} activities",
                x_label="factor  (corrected total / GPS total)",
            )
        )
        print(f"wrote {args.plot}", file=out)
    return 0


def _print_table(measured: list[tuple[str, ActivityStats]], out: Any) -> None:
    header = (
        f"{'file':<12}{'sport':<9}{'factor':>8}{'infl':>8}"
        f"{'cover':>7}{'gaps':>6}{'wiggle':>8}{'lead':>7}{'lag':>6}{'dMove':>7}"
    )
    print(header, file=out)
    print("-" * len(header), file=out)
    for name, s in measured:
        print(
            f"{name[:11]:<12}{s.sport[:8]:<9}"
            f"{_num(s.factor, '.4f'):>8}{_pct(s.inflation):>8}"
            f"{s.gps_coverage * 100:>6.1f}%{s.gap_fraction * 100:>5.1f}%"
            f"{_num(s.wiggle, '.3f'):>8}"
            f"{_num(s.lead_in_s, '.0f'):>6}s{_num(s.start_lag_s, '.0f'):>5}s"
            f"{s.moving_delta_s:>+6.0f}s",
            file=out,
        )


def _num(value: float | None, spec: str) -> str:
    return "-" if value is None else format(value, spec)


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _fetch_command(args: argparse.Namespace, out: Any, err: Any) -> int:
    try:
        pipeline = _build_pipeline(args)
        data = pipeline.fetch(args.activity_id, raw=args.raw)
    except ReckonError as exc:
        print(f"reckon: {exc}", file=err)
        return 1

    if args.output is None:
        _binary(out).write(data)
    else:
        args.output.write_bytes(data)
        print(f"reckon: wrote {args.output}", file=err)
    return 0


def _sync_command(args: argparse.Namespace, out: Any, err: Any) -> int:
    now = dt.datetime.now(tz=dt.UTC)
    since = args.since or _rfc3339(now - dt.timedelta(days=DEFAULT_SINCE_DAYS))
    until = args.until or _rfc3339(now)

    try:
        pipeline = _build_pipeline(args, dry_run=args.dry_run)
        outcomes = pipeline.sync(start_time=since, end_time=until)
    except ReckonError as exc:
        print(f"reckon: {exc}", file=err)
        return 1

    if args.dry_run:
        print("reckon: dry run — nothing uploaded, nothing recorded", file=err)
    for outcome in outcomes:
        print(_outcome_line(outcome), file=out)
    if not outcomes:
        print(f"nothing recorded between {since} and {until}", file=out)
        return 0

    print("", file=out)
    print(
        "  ".join(
            f"{count} {label}" for label, count in sorted(summarise_outcomes(outcomes).items())
        ),
        file=out,
    )
    # A withheld activity is the only outcome a human has to act on: the file was
    # refused, and it is not on Strava. Failures are reported the same way, since
    # both mean the activity is missing.
    return 1 if any(not o.status.on_strava for o in outcomes) else 0


def _outcome_line(outcome: Outcome) -> str:
    factor = "" if outcome.factor is None else f"  factor {outcome.factor:.4f}"
    detail = f"  {outcome.reason}" if outcome.reason else ""
    marker = "=" if not outcome.fresh else " "
    return (
        f"{marker} {outcome.activity_id:<22}{outcome.name[:18]:<19}"
        f"{outcome.status:<15}{factor}{detail}"
    )


def _build_pipeline(args: argparse.Namespace, *, dry_run: bool = False) -> Pipeline:
    """Assemble the pipeline from the environment and the store.

    Configuration lives here rather than in `pipeline.py` so that the pipeline
    takes constructed clients and can be exercised without an environment
    (`PLAN.md` §2). This is the only place in the codebase that reads os.environ.
    """
    transport = args.transport
    store = FileStore(args.store)
    google = token_holder(
        store,
        "google",
        transport=transport,
        token_url=health_api.TOKEN_URL,
        **_credentials("google"),
    )
    strava = token_holder(
        store,
        "strava",
        transport=transport,
        token_url=strava_api.TOKEN_URL,
        **_credentials("strava"),
    )
    return Pipeline(
        health=health_api.GoogleHealth(transport, google),
        strava=strava_api.Strava(transport, strava),
        logs=store,
        dry_run=dry_run,
    )


def _credentials(service: str) -> dict[str, str]:
    id_var, secret_var = _ENV[service]
    values = {
        "client_id": os.environ.get(id_var, ""),
        "client_secret": os.environ.get(secret_var, ""),
    }
    missing = [
        name
        for name, key in ((id_var, "client_id"), (secret_var, "client_secret"))
        if not values[key]
    ]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        raise ReckonError(f"{' and '.join(missing)} {verb} not set in the environment")
    return values


def _binary(stream: Any) -> IO[bytes]:
    """The byte-accepting half of a text stream, or the stream itself if it is already binary."""
    return getattr(stream, "buffer", stream)


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    sys.exit(main())
