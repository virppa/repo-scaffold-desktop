"""PostToolUse: ruff first (mutates), then mypy/bandit/lint-imports in parallel.

Replaces four sequential PostToolUse hooks in `.claude/settings.json` for
`Edit|Write` of any `*.py` file. Cuts per-edit hook latency from ~3-5s to
~slowest-single-tool wall (typically ~1-2s with cold mypy, ~500ms once
WOR-452's dmypy lands).

Phasing:
  Phase 1 (sequential): ruff check --fix, ruff format — both mutate the
                         file, so they must finish before readers run.
  Phase 2 (parallel):    mypy, bandit, lint-imports — pure readers.

Exit-code semantics (preserves the pre-WOR-463 per-hook behavior):
  - ruff failure -> non-zero exit (matches the old `ruff check --fix &&
    ruff format` chain).
  - mypy/bandit/lint-imports failure -> short message printed, exit code
    unaffected (matches the old `<tool> ... || echo "[tool] ..."` pattern
    that always returned 0).
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def _phase1_ruff(file_path: str) -> int:
    """Run ruff check + format sequentially. Returns the worst exit code."""
    if shutil.which("ruff") is None:
        return 0
    check = _run(["ruff", "check", "--fix", file_path])
    fmt = _run(["ruff", "format", file_path])
    if check.stdout:
        sys.stdout.write(check.stdout)
    if check.stderr:
        sys.stderr.write(check.stderr)
    if fmt.stdout:
        sys.stdout.write(fmt.stdout)
    if fmt.stderr:
        sys.stderr.write(fmt.stderr)
    return check.returncode or fmt.returncode


def _phase2_mypy(file_path: str) -> str | None:
    if shutil.which("mypy") is None:
        return None
    proc = _run(["mypy", file_path])
    if proc.returncode != 0:
        return f"[mypy] Type errors in {file_path}"
    return None


def _phase2_bandit(file_path: str) -> str | None:
    if shutil.which("bandit") is None:
        return None
    proc = _run(["bandit", "-q", file_path])
    if proc.returncode != 0:
        return "[bandit] Issues found - run /security-check"
    return None


def _phase2_lint_imports(_file_path: str) -> str | None:
    if shutil.which("lint-imports") is None:
        return None
    proc = _run(["lint-imports"])
    if proc.returncode != 0:
        return (
            "[import-linter] Architecture contract violation - "
            "run lint-imports for details"
        )
    return None


# Stable output order for phase 2 results.
_PHASE2: tuple[tuple[str, Callable[[str], str | None]], ...] = (
    ("mypy", _phase2_mypy),
    ("bandit", _phase2_bandit),
    ("lint-imports", _phase2_lint_imports),
)


def run(file_path: str) -> int:
    """Entry point. Returns ruff exit code; phase 2 failures only log."""
    if not file_path.endswith(".py"):
        return 0
    if not Path(file_path).exists():
        return 0

    ruff_rc = _phase1_ruff(file_path)

    with ThreadPoolExecutor(max_workers=len(_PHASE2)) as ex:
        futures: dict[str, Future[str | None]] = {
            name: ex.submit(fn, file_path) for name, fn in _PHASE2
        }
        # Drain in declaration order so output is stable, not interleaved.
        for name, _ in _PHASE2:
            msg = futures[name].result()
            if msg:
                print(msg)
    return ruff_rc


def main() -> int:
    file_path = os.environ.get("CLAUDE_TOOL_INPUT_FILE_PATH", "")
    if not file_path:
        return 0
    return run(file_path)


if __name__ == "__main__":
    sys.exit(main())
