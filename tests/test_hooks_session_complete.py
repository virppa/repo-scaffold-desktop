"""Tests for the Stop hook at .claude/hooks/check_session_complete.py (WOR-372).

The hook runs as a subprocess (Claude Code invokes it via the settings.json
"command" entry), so these tests exercise it the same way: spawn the script
with a JSON payload on stdin, parse stdout/stderr/exit-code.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "hooks"
    / "check_session_complete.py"
)


def _run_hook(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """Run the hook with the given payload on stdin. Returns the completed process."""
    return subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(  # nosec B603 B607
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def main_repo(tmp_path: Path) -> Path:
    """A fresh main-worktree git repo at tmp_path. NOT a linked worktree."""
    _git(["init", "-q", "-b", "main"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "README.md"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "seed"], cwd=tmp_path)
    return tmp_path


@pytest.fixture
def linked_worktree(main_repo: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A linked worktree (mimics what the watcher creates for a worker session)."""
    wt_path = tmp_path_factory.mktemp("worktree")
    # `git worktree add` requires a branch that doesn't exist yet
    _git(["worktree", "add", "-q", "-b", "feature", str(wt_path)], cwd=main_repo)
    return wt_path


def _write_manifest(
    worktree: Path,
    ticket_id: str = "WOR-282",
    result_json_rel: str = ".claude/artifacts/wor_282/result.json",
) -> Path:
    artifact_dir = worktree / ".claude" / "artifacts" / "wor_282"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "ticket_id": ticket_id,
                "artifact_paths": {
                    "result_json": result_json_rel,
                    "manifest_copy": str(manifest_path.relative_to(worktree)),
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


# ---------------------------------------------------------------------------
# Pass-through cases (hook should NOT block)
# ---------------------------------------------------------------------------


def test_hook_passes_when_not_a_worker_session(main_repo: Path) -> None:
    """Operator session in the main worktree — no manifest — pass through."""
    proc = _run_hook({"cwd": str(main_repo), "stop_hook_active": False})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_passes_when_main_worktree_has_old_manifests(main_repo: Path) -> None:
    """Main worktree with leftover manifest from past worker runs — still pass.

    Without the linked-worktree gate, the hook would erroneously enforce on
    operator sessions that happen to have historical artifacts on disk.
    """
    artifact_dir = main_repo / ".claude" / "artifacts" / "wor_99"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "manifest.json").write_text(
        json.dumps(
            {
                "ticket_id": "WOR-99",
                "artifact_paths": {
                    "result_json": ".claude/artifacts/wor_99/result.json",
                    "manifest_copy": ".claude/artifacts/wor_99/manifest.json",
                },
            }
        ),
        encoding="utf-8",
    )
    proc = _run_hook({"cwd": str(main_repo), "stop_hook_active": False})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_passes_when_all_gates_satisfied(linked_worktree: Path) -> None:
    """Worker session where result.json exists AND working tree is clean — pass."""
    _write_manifest(linked_worktree)
    (linked_worktree / ".claude" / "artifacts" / "wor_282" / "result.json").write_text(
        json.dumps({"ticket_id": "WOR-282", "status": "success"}), encoding="utf-8"
    )
    _git(["add", "-A"], cwd=linked_worktree)
    _git(["commit", "-q", "-m", "Part of WOR-282: complete"], cwd=linked_worktree)

    proc = _run_hook({"cwd": str(linked_worktree), "stop_hook_active": False})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_passes_when_stop_hook_active_even_with_violations(
    linked_worktree: Path,
) -> None:
    """When stop_hook_active is True (already blocked once), do not block again."""
    _write_manifest(linked_worktree)
    # No result.json, dirty tree — would normally block
    proc = _run_hook({"cwd": str(linked_worktree), "stop_hook_active": True})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Block cases (hook MUST block)
# ---------------------------------------------------------------------------


def test_hook_blocks_when_result_json_missing(linked_worktree: Path) -> None:
    """Worker session with no result.json — block with a Write-tool instruction."""
    _write_manifest(linked_worktree)
    proc = _run_hook({"cwd": str(linked_worktree), "stop_hook_active": False})
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block"
    assert "result.json" in decision["reason"]
    assert "Write tool" in decision["reason"]


def test_hook_blocks_when_uncommitted_changes(linked_worktree: Path) -> None:
    """Worker session with result.json but a dirty tree — block on commit."""
    _write_manifest(linked_worktree)
    (linked_worktree / ".claude" / "artifacts" / "wor_282" / "result.json").write_text(
        json.dumps({"ticket_id": "WOR-282", "status": "success"}), encoding="utf-8"
    )
    # Create an unstaged change
    (linked_worktree / "new_file.py").write_text("# dirty", encoding="utf-8")

    proc = _run_hook({"cwd": str(linked_worktree), "stop_hook_active": False})
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block"
    assert "uncommitted changes" in decision["reason"]
    assert "git add -A && git commit" in decision["reason"]
    assert "WOR-282" in decision["reason"]


# ---------------------------------------------------------------------------
# Fail-open cases (hook should NOT block on bad input)
# ---------------------------------------------------------------------------


def test_hook_fails_open_on_malformed_json(tmp_path: Path) -> None:
    """Garbage stdin — hook returns 0 silently (fail open)."""
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input="not valid json {",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_fails_open_when_cwd_is_not_a_git_repo(tmp_path: Path) -> None:
    """Cwd outside any git repo — hook fails open (no enforcement possible)."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    proc = _run_hook({"cwd": str(not_a_repo), "stop_hook_active": False})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_fails_open_on_unreadable_manifest(linked_worktree: Path) -> None:
    """Manifest exists but is malformed — hook does not enforce."""
    artifact_dir = linked_worktree / ".claude" / "artifacts" / "wor_282"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "manifest.json").write_text(
        "this is not valid json", encoding="utf-8"
    )
    proc = _run_hook({"cwd": str(linked_worktree), "stop_hook_active": False})
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
