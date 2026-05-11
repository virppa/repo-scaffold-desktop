"""Tests for the WOR-444 PreToolUse hook — block worker-side PR/push ops."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "hooks"
    / "check_no_worker_pr.py"
)


def _run(
    payload: dict[str, object],
    *,
    cwd: Path,
    watcher_worker: str | None = "1",
) -> tuple[int, str]:
    env = os.environ.copy()
    if watcher_worker is None:
        env.pop("WATCHER_WORKER", None)
    else:
        env["WATCHER_WORKER"] = watcher_worker
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


# ── No-fire paths (must allow) ──────────────────────────────────────────────


def test_operator_session_unblocked(tmp_path: Path) -> None:
    """WATCHER_WORKER unset → operator session → never block."""
    for cmd in ("gh pr create", "gh pr merge --auto", "git push -u origin foo"):
        rc, out = _run(_bash(cmd), cwd=tmp_path, watcher_worker=None)
        assert rc == 0
        assert out.strip() == ""


def test_non_bash_tool_ignored(tmp_path: Path) -> None:
    """Non-Bash tool calls are ignored regardless of payload."""
    payload = {"tool_name": "Edit", "tool_input": {"command": "gh pr create"}}
    rc, out = _run(payload, cwd=tmp_path)
    assert rc == 0
    assert out.strip() == ""


def test_invalid_json_fails_open(tmp_path: Path) -> None:
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


# ── Allowed git/gh commands ─────────────────────────────────────────────────


def test_allowed_git_commands_pass(tmp_path: Path) -> None:
    """Local-only git commands (commit, status, etc.) must always pass."""
    for cmd in (
        "git commit -m 'work'",
        "git status",
        "git diff",
        "git log --oneline",
        "git add foo.py",
        "git fetch origin",  # fetch is fine — only push is blocked
        "git checkout -b feature",
    ):
        rc, out = _run(_bash(cmd), cwd=tmp_path)
        assert rc == 0, f"unexpected block on: {cmd}"
        assert out.strip() == "", f"unexpected output on: {cmd}"


def test_allowed_gh_commands_pass(tmp_path: Path) -> None:
    """Read-only gh queries pass (only create/edit/merge are blocked)."""
    for cmd in (
        "gh pr list --head foo",
        "gh pr view 123",
        "gh pr status",
        "gh issue list",
    ):
        rc, out = _run(_bash(cmd), cwd=tmp_path)
        assert rc == 0, f"unexpected block on: {cmd}"
        assert out.strip() == "", f"unexpected output on: {cmd}"


# ── Blocked paths ───────────────────────────────────────────────────────────


def test_gh_pr_create_blocked(tmp_path: Path) -> None:
    rc, out = _run(_bash("gh pr create --base main"), cwd=tmp_path)
    assert rc == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
    assert "WOR-444" in decision["reason"]
    assert "gh pr create" in decision["reason"]


def test_gh_pr_edit_blocked(tmp_path: Path) -> None:
    rc, out = _run(_bash("gh pr edit 123 --add-label foo"), cwd=tmp_path)
    assert rc == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"


def test_gh_pr_merge_blocked(tmp_path: Path) -> None:
    rc, out = _run(_bash("gh pr merge --auto --squash"), cwd=tmp_path)
    assert rc == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"


def test_git_push_origin_blocked(tmp_path: Path) -> None:
    for cmd in (
        "git push -u origin wor-1-branch",
        "git push origin main",
        "git push",  # bare push to upstream (origin in worktree setup)
        "git push --force origin",
    ):
        rc, out = _run(_bash(cmd), cwd=tmp_path)
        assert rc == 0
        decision = json.loads(out)
        assert decision["decision"] == "block", f"expected block on: {cmd}"
        assert "WOR-444" in decision["reason"]


def test_git_push_to_other_remote_unblocked(tmp_path: Path) -> None:
    """Pushing to a non-origin remote is unusual but not blocked."""
    rc, out = _run(_bash("git push my-fork branch"), cwd=tmp_path)
    assert rc == 0
    assert out.strip() == ""


def test_leading_whitespace_still_blocked(tmp_path: Path) -> None:
    rc, out = _run(_bash("   gh pr create"), cwd=tmp_path)
    assert rc == 0
    decision = json.loads(out)
    assert decision["decision"] == "block"
