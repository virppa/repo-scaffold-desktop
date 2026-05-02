"""Pre-commit hook: validate mock patch() string module paths.

Scans all .py files under tests/ for ``patch("app.` and ``patch('app.`
string literals, extracts the module portion, and verifies that the module
is importable via importlib.util.find_spec().

Exits 0 when all paths are valid, exits 1 with a report of invalid paths.
Does not flag patch.object() calls.
"""

import importlib.util
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

# Match patch("app...) or patch('app...) but NOT patch.object(...)
PATCH_RE = re.compile(r"\bpatch\(['\"]app")

# Capture the full quoted module path from patch("app.X.Y.Z") calls.
VALIDATE_RE = re.compile(r"""patch\(['"]((?:app)(?:\.[\w_]+)+)['"]\)""")


def _validate_module_path(path: str) -> bool:
    """Return True if *path* represents an importable module.

    A patch string like ``"app.cli.generate"`` refers to module ``app.cli``
    (importable) and attribute ``generate``.  ``find_spec`` only accepts
    actual modules — it will fail on ``app.cli.generate`` but succeed on
    ``app.cli``.  This function walks the dotted path from left to right,
    returning True as soon as an importable prefix is found.
    """
    parts = path.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        try:
            if importlib.util.find_spec(candidate) is not None:
                return True
        except (ValueError, AttributeError, ModuleNotFoundError):
            # find_spec raises ValueError for empty module name,
            # AttributeError when the parent package exists but the
            # requested submodule has no __path__ (not a package).
            continue
    return False


def scan_tests_dir(repo_root: Path) -> list[tuple[str, int, str, str]]:
    """Return all invalid patch() paths found under tests/."""
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return []

    results: list[tuple[str, int, str, str]] = []

    for py_file in sorted(tests_dir.rglob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            if not PATCH_RE.search(line):
                continue

            m = VALIDATE_RE.search(line)
            if m is None:
                continue

            module = m.group(1)
            if not _validate_module_path(module):
                results.append(
                    (
                        str(py_file.relative_to(repo_root)),
                        line_no,
                        m.group(0),
                        module,
                    )
                )

    return results


def main() -> int:
    bad = scan_tests_dir(REPO_ROOT)
    if not bad:
        return 0

    print(
        f"check-patch-paths: {len(bad)} invalid patch() path(s) found\n",
        file=sys.stderr,
    )
    for fpath, line_no, raw, module in bad:
        print(
            f"  {fpath}:{line_no}  {raw}\n    -> module '{module}' not found\n",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
