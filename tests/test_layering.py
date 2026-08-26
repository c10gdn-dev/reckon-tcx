"""The layering invariants from PLAN.md §2, enforced rather than documented.

`core/` importing a third-party package, or `pipeline/` reaching for boto3, is
silent until something breaks in a way that is expensive to diagnose. Walking the
AST costs nothing and fails loudly.

Most of this passes trivially today because the modules it guards do not exist
yet. That is the point — the test is in place before the code it constrains.
"""

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "reckon"

# boto3 may appear here and nowhere else.
AWS_ALLOWED = ("stores/dynamo.py", "aws/")


def imported_modules(path: Path) -> set[str]:
    """Absolute module names imported by a file. Relative imports stay in-package."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def python_files(*relative: str) -> list[Path]:
    paths: list[Path] = []
    for part in relative:
        target = SRC / part
        if target.is_dir():
            paths.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            paths.append(target)
    return paths


def test_core_imports_only_stdlib_and_itself() -> None:
    files = python_files("core")
    assert files, "expected core/ to contain modules"
    for path in files:
        for module in imported_modules(path):
            top = module.split(".")[0]
            assert top in sys.stdlib_module_names or module.startswith("reckon.core"), (
                f"{path.relative_to(SRC)} imports {module!r}; core/ is stdlib-only and may "
                f"only import from reckon.core"
            )


def test_core_and_pipeline_are_aws_free() -> None:
    for path in python_files("core", "pipeline.py", "pipeline"):
        for module in imported_modules(path):
            top = module.split(".")[0]
            assert top not in {"boto3", "botocore"}, (
                f"{path.relative_to(SRC)} imports {module!r}; boto3 belongs only in "
                f"{' and '.join(AWS_ALLOWED)}"
            )


def test_boto3_appears_only_where_it_is_allowed() -> None:
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if any(relative.startswith(allowed) for allowed in AWS_ALLOWED):
            continue
        for module in imported_modules(path):
            assert module.split(".")[0] not in {"boto3", "botocore"}, (
                f"{relative} imports {module!r}, which is outside {' and '.join(AWS_ALLOWED)}"
            )
