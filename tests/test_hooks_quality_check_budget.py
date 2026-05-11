"""Tests for the WOR-421 PreToolUse hook — quality-check command budget.

The hook lives at .claude/hooks/check_quality_check_budget.py. Each test
invokes the hook via subprocess, feeding a JSON payload on stdin and
asserting on:
  - exit code 0 (hook never fails the call itself; block is via JSON output)
  - JSON output containing decision="block" when over budget
  - absence of JSON output when allowed
  - counter file state in the cwd
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

# Hook script path relative to repo root (which is also cwd for pytest).
_HOOK = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "hooks"
    / "check_quality_check_budget.py"
)


def _run(
    payload: dict[str, object],
    *,
    cwd: Path,
    watcher_worker: str | None = "1",
    budget: str | None = "4",
) -> tuple[int, str]:
    """Invoke the hook, return (returncode, stdout)."""
    env = os.environ.copy()
    if watcher_worker is None:
        env.pop("WATCHER_WORKER", None)
    else:
        env["WATCHER_WORKER"] = watcher_worker
    if budget is None:
        env.pop("WATCHER_QUALITY_CHECK_BUDGET", None)
    else:
        env["WATCHER_QUALITY_CHECK_BUDGET"] = budget
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        text=True,
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    return proc.returncode, proc.stdout


def _bash(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _counter_path(cwd: Path) -> Path:
    return cwd / ".claude" / ".quality_check_count"


# ── No-fire paths (must allow) ──────────────────────────────────────────────


def test_no_block_when_not_in_worker_context(tmp_path: Path) -> None:
    """WATCHER_WORKER unset → operator session → never block."""
    for _ in range(20):
        rc, out = _run(_bash("pytest tests/"), cwd=tmp_path, watcher_worker=None)
    assert rc == 0
    assert out.strip() == ""
    assert not _counter_path(tmp_path).exists()


def test_no_block_for_non_bash_tool(tmp_path: Path) -> None:
    """Non-Bash tool calls are ignored regardless of command content."""
    payload = {"tool_name": "Edit", "tool_input": {"command": "pytest tests/"}}
    rc, out = _run(payload, cwd=tmp_path)
    assert rc == 0
    assert out.strip() == ""


def test_no_block_for_non_quality_bash_command(tmp_path: Path) -> None:
    """Bash commands that don't start with a quality-check tool pass through."""
    for cmd in ("git status", "ls -la", "echo hi", "python script.py", "cd subdir"):
        rc, out = _run(_bash(cmd), cwd=tmp_path)
        assert rc == 0
        assert out.strip() == ""
    assert not _counter_path(tmp_path).exists()


def test_no_block_on_invalid_json(tmp_path: Path) -> None:
    """Invalid JSON on stdin → fail-open (exit 0, no output)."""
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(_HOOK)],
        input="not valid json",
        text=True,
        cwd=tmp_path,
        env={**os.environ, "WATCHER_WORKER": "1"},
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── Counter + budget paths ──────────────────────────────────────────────────


def test_under_budget_increments_counter_and_allows(tmp_path: Path) -> None:
    """First 4 invocations (budget=4) all pass and increment the counter."""
    for i in range(1, 5):
        rc, out = _run(_bash("pytest tests/"), cwd=tmp_path, budget="4")
        assert rc == 0
        assert out.strip() == ""
        assert _counter_path(tmp_path).read_text(encoding="utf-8") == str(i)


def test_over_budget_blocks(tmp_path: Path) -> None:
    """5th invocation with budget=4 is blocked with a structured decision."""
    # Pre-populate counter to budget
    _counter_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _counter_path(tmp_path).write_text("4", encoding="utf-8")

    rc, out = _run(_bash("pytest tests/"), cwd=tmp_path, budget="4")
    assert rc == 0  # hook itself always exits 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "Hook-trust violation" in decision["reason"]
    assert "WOR-421" in decision["reason"]
    # Counter does NOT advance once blocked
    assert _counter_path(tmp_path).read_text(encoding="utf-8") == "4"


def test_all_5_quality_tokens_count_against_budget(tmp_path: Path) -> None:
    """ruff, mypy, pytest, bandit, lint-imports all count toward the budget."""
    commands = (
        "ruff check .",
        "mypy app/",
        "pytest tests/",
        "bandit -r app/",
        "lint-imports",
    )
    for cmd in commands:
        rc, _ = _run(_bash(cmd), cwd=tmp_path, budget="5")
        assert rc == 0
    assert _counter_path(tmp_path).read_text(encoding="utf-8") == "5"

    # The 6th any-quality call now blocks
    rc, out = _run(_bash("ruff format ."), cwd=tmp_path, budget="5")
    assert rc == 0
    assert "block" in out


def test_first_token_match_with_leading_whitespace(tmp_path: Path) -> None:
    """Leading whitespace + chained command — counted by first token."""
    rc, _ = _run(_bash("   pytest tests/test_foo.py && echo ok"), cwd=tmp_path)
    assert rc == 0
    assert _counter_path(tmp_path).read_text(encoding="utf-8") == "1"


def test_token_substring_does_not_match(tmp_path: Path) -> None:
    """`ruffles` should not count as `ruff` — exact first-token match only."""
    # `pytester` isn't a real tool but the parser doesn't know that; it just
    # checks the first space-delimited token. So "ruffles" is NOT "ruff".
    rc, _ = _run(_bash("ruffles run something"), cwd=tmp_path)
    assert rc == 0
    assert not _counter_path(tmp_path).exists()


def test_default_budget_4_when_env_missing(tmp_path: Path) -> None:
    """Missing WATCHER_QUALITY_CHECK_BUDGET → defaults to 4."""
    _counter_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _counter_path(tmp_path).write_text("4", encoding="utf-8")
    rc, out = _run(_bash("pytest tests/"), cwd=tmp_path, budget=None)
    assert rc == 0
    assert "block" in out


def test_invalid_budget_env_falls_back_to_default(tmp_path: Path) -> None:
    """Non-integer budget → falls back to default 4."""
    _counter_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _counter_path(tmp_path).write_text("4", encoding="utf-8")
    rc, out = _run(_bash("pytest tests/"), cwd=tmp_path, budget="not-a-number")
    assert rc == 0
    assert "block" in out  # 4 used >= 4 default → block


# ── build_worker_env integration ────────────────────────────────────────────


def test_build_worker_env_sets_quality_check_budget() -> None:
    """build_worker_env writes WATCHER_QUALITY_CHECK_BUDGET when passed."""
    from app.core.watcher.watcher_helpers import build_worker_env

    env = build_worker_env(
        "local",
        {"FOO": "bar"},
        quality_check_budget=4,
    )
    assert env["WATCHER_QUALITY_CHECK_BUDGET"] == "4"
    assert env["WATCHER_WORKER"] == "1"


def test_build_worker_env_omits_budget_when_none() -> None:
    """When quality_check_budget is None, the env var is not set."""
    from app.core.watcher.watcher_helpers import build_worker_env

    env = build_worker_env("local", {"FOO": "bar"})
    assert "WATCHER_QUALITY_CHECK_BUDGET" not in env
