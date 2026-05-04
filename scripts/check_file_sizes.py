"""Pre-commit hook: enforce per-file LOC thresholds for Python sources (WOR-377).

Migrated from the prose checklist in `.claude/commands/finalize-ticket.md`
and `/close-epic.md` so drift is caught at commit time, not just at the
end of a ticket. Two threshold tables — one for production code and one
for tests, recalibrated post-WOR-368 based on the observation that test
files are read less often by LLMs and parallel-worker isolation matters
less for fixtures.

Production (app/, scripts/): ADVISORY 500 / RECOMMEND 700 / BLOCK 1200.
Tests (tests/):              ADVISORY 800 / RECOMMEND 1200 / BLOCK 2000.

Behaviour:

- ADVISORY  — informational stderr line; commit proceeds.
- RECOMMEND — warning stderr line; commit proceeds.
- BLOCK     — error stderr line; commit fails (exit 1).

Bypass with ``git commit --no-verify`` when an incidental edit to an
already-oversized file genuinely shouldn't grow the ticket scope.

Wire in ``.pre-commit-config.yaml``::

    - repo: local
      hooks:
        - id: check-file-sizes
          name: check-file-sizes
          language: system
          entry: python scripts/check_file_sizes.py
          types: [python]
"""

from __future__ import annotations

import sys
from pathlib import Path

PROD_ADVISORY = 500
PROD_RECOMMEND = 700
PROD_BLOCK = 1200

TEST_ADVISORY = 800
TEST_RECOMMEND = 1200
TEST_BLOCK = 2000


def is_test_file(path: str) -> bool:
    """A path is a test file if any path part is `tests`."""
    parts = Path(path).parts
    return "tests" in parts


def thresholds_for(path: str) -> tuple[int, int, int]:
    """Return (advisory, recommend, block) thresholds for the path."""
    if is_test_file(path):
        return TEST_ADVISORY, TEST_RECOMMEND, TEST_BLOCK
    return PROD_ADVISORY, PROD_RECOMMEND, PROD_BLOCK


def classify(loc: int, path: str) -> str | None:
    """Return 'advisory'|'recommend'|'block' or None if under all thresholds."""
    advisory, recommend, block = thresholds_for(path)
    if loc >= block:
        return "block"
    if loc >= recommend:
        return "recommend"
    if loc >= advisory:
        return "advisory"
    return None


def count_lines(path: Path) -> int:
    """Count total lines in the file. Returns 0 on read errors."""
    try:
        with path.open(encoding="utf-8") as f:
            return sum(1 for _ in f)
    except (OSError, UnicodeDecodeError):
        return 0


def check_file(path_str: str) -> tuple[int, str | None]:
    """Return (loc, severity) for a single path."""
    path = Path(path_str)
    if not path.exists() or path.suffix != ".py":
        return (0, None)
    loc = count_lines(path)
    return (loc, classify(loc, path_str))


def _format_message(path: str, loc: int, severity: str) -> str:
    advisory, recommend, block = thresholds_for(path)
    tier_label = "test" if is_test_file(path) else "production"
    if severity == "block":
        return (
            f"[file-size:BLOCK] {path} is {loc} LOC "
            f"(over the {tier_label} BLOCK threshold of {block}). "
            "Split into themed siblings before committing, or pass "
            "--no-verify if this edit doesn't add scope."
        )
    if severity == "recommend":
        return (
            f"[file-size:RECOMMEND] {path} is {loc} LOC "
            f"(over the {tier_label} RECOMMEND threshold of {recommend}). "
            "Consider splitting on the next ticket boundary."
        )
    return (
        f"[file-size:advisory] {path} is {loc} LOC "
        f"(over the {tier_label} ADVISORY threshold of {advisory})."
    )


def main(argv: list[str]) -> int:
    """Entry point. Returns 1 if any file is over BLOCK; 0 otherwise."""
    failed = False
    for path in argv:
        loc, severity = check_file(path)
        if severity is None:
            continue
        print(_format_message(path, loc, severity), file=sys.stderr)
        if severity == "block":
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
