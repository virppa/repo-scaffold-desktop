"""PreToolUse hook — enforce the per-session quality-check command budget (WOR-421).

WOR-274 shipped a hard-rule prose in implement-ticket.md step 3 forbidding
manual invocations of ruff / mypy / pytest / bandit / lint-imports. The
prose didn't stick — WOR-337 evidence: 21 manual invocations in one
session despite the rule. This hook converts the rule into enforcement.

Trigger conditions (all must hold):
  1. `WATCHER_WORKER=1` in env  — only fire in worker subprocess sessions;
     operator-invoked sessions running `/finalize-ticket` are unblocked.
  2. `tool_name == "Bash"`.
  3. First token of the Bash command (after lstrip) is one of:
     ruff, mypy, pytest, bandit, lint-imports.
  4. The cumulative count of such invocations in this session exceeds
     `WATCHER_QUALITY_CHECK_BUDGET` (default 4 = len(required_checks)
     in current production manifests).

The counter is persisted to `.claude/.quality_check_count` in cwd
(worktree-local — each worker session gets its own counter naturally
because each worker runs in its own worktree).

Fails open — any JSON/IO error → exit 0 (allow). We never want this hook
to break a worker session by surprise.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Same token set as app/core/watcher/watcher_log_parsing.py::_HOOK_VIOLATION_TOKENS
# (WOR-274). Keep these in sync — the measurement layer counts the same tokens.
_QUALITY_CHECK_TOKENS = frozenset(
    ("ruff", "mypy", "pytest", "bandit", "lint-imports"),
)

_COUNTER_FILE = Path(".claude") / ".quality_check_count"
_DEFAULT_BUDGET = 4  # len(required_checks) in current production manifests


def _read_count() -> int:
    """Read the per-session counter; return 0 on any failure (fail open)."""
    try:
        return int(_COUNTER_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_count(n: int) -> None:
    """Persist the counter; silent on failure (we don't want hook IO errors
    to surface to the worker)."""
    try:
        _COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COUNTER_FILE.write_text(str(n), encoding="utf-8")
    except OSError:
        pass


def _block(used: int, budget: int) -> int:
    """Emit a structured block decision in the format Claude Code's hook
    runtime understands, with a clear next-step message."""
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    f"Hook-trust violation (WOR-421): the per-session "
                    f"quality-check command budget of {budget} has been "
                    f"used (this would be invocation #{used + 1}). "
                    f"ruff / mypy / pytest / bandit / lint-imports are "
                    f"already run automatically by PostToolUse hooks "
                    f"after every Edit/Write to a .py file, and the "
                    f"watcher's required_checks step runs them once at "
                    f"finalize time. Manual invocations are redundant. "
                    f"If you genuinely need to inspect a specific test "
                    f"result, read the PostToolUse hook output in the "
                    f"prior tool result. See CLAUDE.md 'Worker efficiency' "
                    f"and .claude/commands/implement-ticket.md step 3."
                ),
            }
        )
    )
    return 0


def main() -> int:
    # Worker-context detection: only fire when the watcher launched this
    # Claude Code session. Operator sessions get a free pass so /finalize-
    # ticket and ad-hoc spot-runs still work.
    if os.environ.get("WATCHER_WORKER") != "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # fail open

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = payload.get("tool_input", {}).get("command", "")
    if not isinstance(cmd, str):
        return 0
    stripped = cmd.lstrip()
    if not stripped:
        return 0

    first_token = stripped.split()[0]
    if first_token not in _QUALITY_CHECK_TOKENS:
        return 0

    # Token is a quality-check command. Count it and decide.
    used = _read_count()
    try:
        budget = int(os.environ.get("WATCHER_QUALITY_CHECK_BUDGET", _DEFAULT_BUDGET))
    except ValueError:
        budget = _DEFAULT_BUDGET

    if used >= budget:
        return _block(used, budget)

    _write_count(used + 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
