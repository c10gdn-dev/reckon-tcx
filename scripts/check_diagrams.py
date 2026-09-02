#!/usr/bin/env python3
"""Fail if a committed diagram SVG no longer matches its .puml source.

Compares the *text* of the two renderings rather than their bytes. Byte equality
would be the obvious check and does not survive CI: plantuml derives box geometry
from font metrics, so a runner without the fonts this machine has produces a
different-but-correct SVG for an unchanged diagram. Measured, not assumed —
rendering one diagram with two fonts gives two different files.

What a text comparison still catches is the failure that actually happens: a
.puml edited and committed without regenerating its .svg. Editing a diagram means
editing its labels, and those are compared exactly, in order.

What it does not catch is a diagram that renders perfectly and describes
something the code no longer does. Nothing catches that but reading it.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TEXT = re.compile(r"<text[^>]*>([^<]*)</text>")


def labels(svg: str) -> list[str]:
    """Every rendered string, in document order."""
    return [t.strip() for t in TEXT.findall(svg) if t.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("directory", type=Path, nargs="?", default=Path("docs/diagrams"))
    parser.add_argument("--plantuml", default="plantuml", help="renderer command")
    args = parser.parse_args(argv)

    sources = sorted(args.directory.glob("*.puml"))
    if not sources:
        print(f"no .puml files in {args.directory}", file=sys.stderr)
        return 1

    stale: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [*args.plantuml.split(), "-tsvg", "-o", tmp, *map(str, sources)],
            check=True,
            capture_output=True,
        )
        for source in sources:
            committed = source.with_suffix(".svg")
            fresh = Path(tmp) / committed.name
            if not fresh.exists():
                print(f"{source}: produced no SVG", file=sys.stderr)
                stale.append(source.name)
                continue
            if not committed.exists():
                print(f"{source}: {committed} is missing; run `make diagrams`", file=sys.stderr)
                stale.append(source.name)
                continue
            if labels(committed.read_text()) != labels(fresh.read_text()):
                print(
                    f"{source}: {committed} does not match its source; run `make diagrams`",
                    file=sys.stderr,
                )
                stale.append(source.name)

    if stale:
        return 1
    print(f"{len(sources)} diagram(s) match their sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
