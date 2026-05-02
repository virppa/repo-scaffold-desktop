"""Tests for app.core.watcher.watcher_worktrees."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths
from app.core.watcher.watcher_types import ActiveWorker
from app.core.watcher.watcher_worktrees import (
    backup_plan_files,
    cleanup_orphaned_worktrees,
    cleanup_worktree,
    commit_wip_state,
    copy_manifest_to_worktree,
    create_worktree,
    preserve_worker_artifacts,
    rebase_worktree_from_base,
    restore_plan_files,
    squash_wip_commits,
    write_worker_pytest_config,
)
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# create_worktree
# ---------------------------------------------------------------------------


def test_create_worktree_happy_path(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        base_branch="main",
        objective="Test",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-10"),
    )

    # create_worktree uses repo_root.parent / "worktrees" / name
    worktree_path = tmp_path.parent / "worktrees" / "wor-10-test"

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "app.core.watcher.watcher_worktrees.rebase_worktree_from_base",
        ) as mock_rebase,
    ):
        result = create_worktree(tmp_path, manifest)

    assert result == worktree_path
    assert mock_run.call_count == 1
    assert mock_rebase.call_count == 1
    mock_rebase.assert_called_once_with(worktree_path, "main")


def test_create_worktree_uses_worktree_name_when_present(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        base_branch="main",
        objective="Test",
        worktree_name="custom-worktree",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-10"),
    )

    expected_path = tmp_path.parent / "worktrees" / "custom-worktree"

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "app.core.watcher.watcher_worktrees.rebase_worktree_from_base",
        ),
    ):
        result = create_worktree(tmp_path, manifest)

    assert result == expected_path
    mock_run.assert_called_once()
    # Check the path argument in the subprocess call
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert str(expected_path) in cmd


def test_create_worktree_raises_on_path_traversal() -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10/../../../etc",
        base_branch="main",
        objective="Test",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-10"),
    )

    with pytest.raises(ValueError, match="Invalid worktree name"):
        create_worktree(Path("/tmp"), manifest)


# ---------------------------------------------------------------------------
# rebase_worktree_from_base
# ---------------------------------------------------------------------------


def test_rebase_worktree_from_base_warns_on_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "git", stderr="conflict")

    with (
        patch("subprocess.run", side_effect=_raise),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_worktrees"),
    ):
        rebase_worktree_from_base(tmp_path, "some-epic-branch")

    assert any("Could not rebase" in r.message for r in caplog.records)


def test_rebase_worktree_from_base_success(tmp_path: Path) -> None:
    with patch("subprocess.run") as mock_run:
        rebase_worktree_from_base(tmp_path, "main")

    assert mock_run.call_count == 2
    # First call: git fetch origin <base_branch>
    fetch_call = mock_run.call_args_list[0]
    assert "fetch" in fetch_call[0][0]
    assert "origin" in fetch_call[0][0]
    assert "main" in fetch_call[0][0]
    # Second call: git rebase origin/<base_branch>
    rebase_call = mock_run.call_args_list[1]
    assert "rebase" in rebase_call[0][0]
    assert "origin/main" in rebase_call[0][0]


# ---------------------------------------------------------------------------
# copy_manifest_to_worktree
# ---------------------------------------------------------------------------


def test_copy_manifest_to_worktree_copies_manifest(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        base_branch="main",
        worker_branch="wor-10-test",
        objective="Test",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-10"),
    )

    # Create the source manifest file
    src_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    src_dir.mkdir(parents=True)
    src_manifest = src_dir / "manifest.json"
    src_manifest.write_text('{"ticket_id": "WOR-10"}')

    worktree_path = tmp_path / "worktrees" / "wor-10"
    worktree_path.mkdir(parents=True)

    copy_manifest_to_worktree(tmp_path, manifest, worktree_path)

    dest = worktree_path / ".claude" / "artifacts" / "wor_10" / "manifest.json"
    assert dest.exists()
    assert dest.read_text() == '{"ticket_id": "WOR-10"}'


def test_copy_manifest_to_worktree_copies_last_failure_when_present(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        base_branch="main",
        worker_branch="wor-10-test",
        objective="Test",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-10"),
    )

    src_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    src_dir.mkdir(parents=True)
    (src_dir / "manifest.json").write_text("{}")
    (src_dir / "last_failure.json").write_text('{"failed_at": "2026-01-01"}')

    worktree_path = tmp_path / "worktrees" / "wor-10"
    worktree_path.mkdir(parents=True)

    copy_manifest_to_worktree(tmp_path, manifest, worktree_path)

    failure_file = (
        worktree_path / ".claude" / "artifacts" / "wor_10" / "last_failure.json"
    )
    assert failure_file.exists()
    assert failure_file.read_text() == '{"failed_at": "2026-01-01"}'


def test_copy_manifest_to_worktree_skips_last_failure_when_absent(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        base_branch="main",
        worker_branch="wor-10-test",
        objective="Test",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-10"),
    )

    src_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    src_dir.mkdir(parents=True)
    (src_dir / "manifest.json").write_text("{}")
    # No last_failure.json

    worktree_path = tmp_path / "worktrees" / "wor-10"
    worktree_path.mkdir(parents=True)

    copy_manifest_to_worktree(tmp_path, manifest, worktree_path)

    # Should not raise, failure file should not exist
    dest_dir = worktree_path / ".claude" / "artifacts" / "wor_10"
    assert not (dest_dir / "last_failure.json").exists()


# ---------------------------------------------------------------------------
# backup_plan_files
# ---------------------------------------------------------------------------


def test_backup_plan_files_moves_md_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Create fake plan files at tmp_path/.claude/plans/ (so Path("plans")
    # resolved from tmp_path home will find them via glob).
    plans_dir = tmp_path / ".claude" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "plan1.md").write_text("plan 1 content")
    (plans_dir / "plan2.md").write_text("plan 2 content")
    # Also create a non-.md file to ensure it's skipped
    (plans_dir / "notes.txt").write_text("not a plan")

    backup_dir = tmp_path / ".claude" / "plans_worker_backup"
    backup_dir.mkdir()

    def fake_home() -> Path:
        return tmp_path

    monkeypatch.setattr(Path, "home", staticmethod(fake_home))

    # The module uses Path.home() so we need to also patch Path.home in the module
    with patch("app.core.watcher.watcher_worktrees.Path.home", return_value=tmp_path):
        moved = backup_plan_files()

    assert len(moved) == 2
    for p in moved:
        assert p.parent == backup_dir
        assert p.name in ("plan1.md", "plan2.md")


def test_backup_plan_files_no_op_when_plans_dir_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_home() -> Path:
        return Path("/nonexistent")

    with patch(
        "app.core.watcher.watcher_worktrees.Path.home",
        return_value=Path("/nonexistent"),
    ):
        result = backup_plan_files()

    assert result == []


# ---------------------------------------------------------------------------
# restore_plan_files
# ---------------------------------------------------------------------------


def test_restore_plan_files_moves_files_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backup_dir = tmp_path / "plans_worker_backup"
    backup_dir.mkdir()
    moved_file = backup_dir / "plan1.md"
    moved_file.write_text("restored content")

    plans_dir = tmp_path / ".claude" / "plans"
    plans_dir.mkdir(parents=True)

    backed_up = [moved_file]

    with patch("app.core.watcher.watcher_worktrees.Path.home", return_value=tmp_path):
        restore_plan_files(backed_up)

    restored = plans_dir / "plan1.md"
    assert restored.exists()
    assert restored.read_text() == "restored content"
    assert not moved_file.exists()


def test_restore_plan_files_no_op_on_empty_list() -> None:
    with patch("app.core.watcher.watcher_worktrees.Path.home") as mock_home:
        restore_plan_files([])
        # home() should not be called for empty list
        mock_home.assert_not_called()


# ---------------------------------------------------------------------------
# write_worker_pytest_config
# ---------------------------------------------------------------------------


def test_write_worker_pytest_config(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir(parents=True)

    write_worker_pytest_config(worktree_path)

    config_file = worktree_path / "pytest.ini"
    assert config_file.exists()
    assert config_file.read_text() == "[pytest]\naddopts = --tb=short\n"


# ---------------------------------------------------------------------------
# preserve_worker_artifacts
# ---------------------------------------------------------------------------


def test_preserve_worker_artifacts_copies_log_and_result(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")

    worktree = tmp_path / "worktrees" / "wor-10"
    worktree.mkdir(parents=True)

    log_src = worktree / ".claude" / "worker_wor-10.log"
    log_src.parent.mkdir(parents=True)
    log_src.write_text("log content")

    result_src = worktree / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_src.parent.mkdir(parents=True)
    result_src.write_text('{"status": "success"}')

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )
    preserve_worker_artifacts(tmp_path, worker)

    artifact_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    assert (artifact_dir / "worker_wor-10.log").read_text() == "log content"
    assert (artifact_dir / "result.json").read_text() == '{"status": "success"}'


def test_preserve_worker_artifacts_missing_result_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")

    worktree = tmp_path / "worktrees" / "wor-10"
    worktree.mkdir(parents=True)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )

    with caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_worktrees"):
        preserve_worker_artifacts(tmp_path, worker)

    assert any("No result artifact" in r.message for r in caplog.records)


def test_preserve_worker_artifacts_handles_last_failure(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")

    worktree = tmp_path / "worktrees" / "wor-10"
    worktree.mkdir(parents=True)

    log_src = worktree / ".claude" / "worker_wor-10.log"
    log_src.parent.mkdir(parents=True)
    log_src.write_text("log")

    # Create a result (so no warning about missing result)
    result_src = worktree / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_src.parent.mkdir(parents=True)
    result_src.write_text('{"status": "success"}')

    # Create last_failure.json in the worktree artifact dir
    wt_failure = worktree / ".claude" / "artifacts" / "wor_10" / "last_failure.json"
    wt_failure.write_text('{"failed_at": "2026-01-01"}')

    # Also create a pre-existing last_failure.json in the repo artifact dir
    repo_failure = tmp_path / ".claude" / "artifacts" / "wor_10" / "last_failure.json"
    repo_failure.parent.mkdir(parents=True)
    repo_failure.write_text('{"old": "data"}')

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )

    preserve_worker_artifacts(tmp_path, worker)

    # The last_failure.json should be copied from the worktree
    assert repo_failure.exists()
    assert repo_failure.read_text() == '{"failed_at": "2026-01-01"}'


# ---------------------------------------------------------------------------
# cleanup_worktree
# ---------------------------------------------------------------------------


def test_cleanup_worktree_success(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _no_error(*args: object, **kwargs: object) -> MagicMock:
        mock = MagicMock()
        mock.returncode = 0
        return mock

    with patch("subprocess.run", side_effect=_no_error):
        cleanup_worktree(tmp_path, tmp_path)


def test_cleanup_worktree_logs_warning_on_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "git", stderr="failed to remove")

    with (
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_worktrees"),
        patch("subprocess.run", side_effect=_raise),
    ):
        cleanup_worktree(tmp_path, tmp_path)

    assert any("Failed to remove" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# cleanup_orphaned_worktrees
# ---------------------------------------------------------------------------


def test_cleanup_orphaned_worktrees_removes_subdirs(tmp_path: Path) -> None:
    # IMPORTANT PATH INVARIANT: worktrees live at repo_root.parent / 'worktrees',
    # NOT inside the repo. So repo_root = tmp_path / 'repo' and the worktrees
    # dir becomes tmp_path / 'worktrees'.
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    worktree_dir_a = tmp_path / "worktrees" / "some-branch-a"
    worktree_dir_a.mkdir(parents=True)
    worktree_dir_b = tmp_path / "worktrees" / "some-branch-b"
    worktree_dir_b.mkdir(parents=True)

    with (
        patch("app.core.watcher.watcher_worktrees.cleanup_worktree") as mock_cleanup,
    ):
        cleanup_orphaned_worktrees(repo_root)

    assert mock_cleanup.call_count == 2
    mock_cleanup.assert_any_call(repo_root, worktree_dir_a)
    mock_cleanup.assert_any_call(repo_root, worktree_dir_b)


def test_cleanup_orphaned_worktrees_skips_files(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir(parents=True)
    # Create a regular file (not a dir) — should be skipped
    (worktrees_dir / "readme.md").write_text("not a worktree")

    with (
        patch("app.core.watcher.watcher_worktrees.cleanup_worktree") as mock_cleanup,
    ):
        cleanup_orphaned_worktrees(repo_root)

    mock_cleanup.assert_not_called()


def test_cleanup_orphaned_worktrees_no_op_when_base_absent(
    tmp_path: Path,
) -> None:
    with patch(
        "app.core.watcher.watcher_worktrees.cleanup_worktree",
    ) as mock_cleanup:
        cleanup_orphaned_worktrees(tmp_path)

    mock_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# commit_wip_state
# ---------------------------------------------------------------------------


def _completed_process(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    """Return a CompletedProcess matching subprocess.run output."""
    return subprocess.CompletedProcess(
        args="", returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_commit_wip_state_creates_and_pushes_on_dirty_tree(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Create a dummy file so the tree is dirty
    (tmp_path / "test.txt").write_text("dirty content")

    # status --porcelain → non-empty (tree is dirty)
    # add -A → success
    # commit → success
    # push → success
    # rev-parse HEAD → returns short SHA
    sha = "a1b2c3d4"
    mock_results = [
        _completed_process(stdout="M test.txt\n"),  # status --porcelain
        _completed_process(),  # add -A
        _completed_process(),  # commit
        _completed_process(),  # push
        _completed_process(stdout=sha + "\n"),  # rev-parse HEAD
    ]
    call_count = [0]

    def _side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        idx = call_count[0]
        call_count[0] += 1
        return mock_results[idx]

    with (
        patch(
            "app.core.watcher.watcher_worktrees.subprocess.run",
            side_effect=_side_effect,
        ),
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_worktrees"),
    ):
        result = commit_wip_state(tmp_path, "WOR-10", "wor-10-test")

    assert result == sha
    assert call_count[0] == 5
    assert any("WIP commit" in r.message for r in caplog.records)
    assert any("a1b2c3d4" in r.message for r in caplog.records)


def test_commit_wip_state_no_op_on_clean_tree(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch("app.core.watcher.watcher_worktrees.subprocess.run") as mock_run,
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_worktrees"),
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args="", returncode=0, stdout="", stderr=""
        )
        result = commit_wip_state(Path("/tmp"), "WOR-10", "wor-10-test")

    assert result is None
    assert any("no wip commit needed" in r.message for r in caplog.records)


def test_commit_wip_state_returns_none_on_git_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # status → dirty, add → ok, commit → fails (check=True raises)
    # The mock must raise CalledProcessError because check=True only raises
    # for actual exceptions, not for returncode being set on a CompletedProcess.
    commit_raises = [False]
    call_count = [0]

    def _side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        # 1st call: status --porcelain → dirty (check=False, no raise)
        # 2nd call: add -A → success
        # 3rd call: commit → raises CalledProcessError (check=True)
        idx = call_count[0]
        call_count[0] += 1
        if idx == 0:
            return _completed_process(stdout="M test.txt\n")
        if idx == 2 and not commit_raises[0]:
            commit_raises[0] = True
            raise subprocess.CalledProcessError(
                1, "git commit", stderr="fatal: not a git repo"
            )
        return _completed_process()

    caplog.set_level(logging.WARNING, logger="app.core.watcher.watcher_worktrees")
    with patch(
        "app.core.watcher.watcher_worktrees.subprocess.run",
        side_effect=_side_effect,
    ):
        result = commit_wip_state(tmp_path, "WOR-10", "wor-10-test")

    assert result is None
    assert any("WIP commit failed" in r.message for r in caplog.records)


def test_commit_wip_state_returns_none_on_push_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # status → dirty, add → ok, commit → ok, push → fail
    mock_results = [
        _completed_process(stdout="M test.txt\n"),  # status
        _completed_process(),  # add
        _completed_process(),  # commit
        _completed_process(
            returncode=1, stderr="remote: Authentication failed"
        ),  # push
    ]
    call_count = [0]

    def _side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        idx = call_count[0]
        call_count[0] += 1
        return mock_results[idx]

    with (
        patch(
            "app.core.watcher.watcher_worktrees.subprocess.run",
            side_effect=_side_effect,
        ),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_worktrees"),
    ):
        result = commit_wip_state(tmp_path, "WOR-10", "wor-10-test")

    assert result is None
    assert any("wip commit not pushed" in r.message for r in caplog.records)


def test_commit_wip_state_skips_commit_when_staged_but_not_committed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # git status --porcelain returns empty output but tree actually has
    # untracked files that are already ignored.  We simulate: status returns
    # empty → no-op.  The function never reaches git add/commit.
    with (
        patch(
            "app.core.watcher.watcher_worktrees.subprocess.run",
        ) as mock_run,
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_worktrees"),
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args="", returncode=0, stdout="", stderr=""
        )
        result = commit_wip_state(tmp_path, "WOR-10", "wor-10-test")

    assert result is None
    assert any("no wip commit needed" in r.message for r in caplog.records)


def test_commit_wip_state_handles_status_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # git status fails (non-zero returncode) → log warning and return None
    # status uses check=False so it returns a CompletedProcess, not a
    # CalledProcessError — the error is handled manually in the function.
    with (
        patch(
            "app.core.watcher.watcher_worktrees.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args="git status",
                returncode=1,
                stderr="error: invalid option",
                stdout="",
            ),
        ),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_worktrees"),
    ):
        result = commit_wip_state(tmp_path, "WOR-10", "wor-10-test")

    assert result is None
    assert any("git status failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# squash_wip_commits
# ---------------------------------------------------------------------------


def test_squash_wip_commits_squashes_correctly(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When wip commits exist, they are squashed into a single commit."""
    sha = "a1b2c3d4e5f6"  # pragma: allowlist secret — fake SHA

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        cmd = args[0] if args else kwargs.get("args", [])
        if "merge-base" in cmd:
            return _completed_process(stdout=sha + "\n")
        if "log" in cmd:
            # Return wip commits in git log --oneline format
            return _completed_process(
                stdout="aaaaaaaa wip: WOR-10 implementation\n"
                "bbbbbbbb wip: WOR-10 tests written\n"
            )
        if "reset" in cmd:
            return _completed_process()
        if "commit" in cmd:
            return _completed_process()
        if "push" in cmd:
            return _completed_process()
        if "rev-parse" in cmd:
            return _completed_process(stdout="deadbeef\n")
        return _completed_process()

    with (
        patch(
            "app.core.watcher.watcher_worktrees.subprocess.run",
            side_effect=fake_run,
        ),
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_worktrees"),
    ):
        result = squash_wip_commits(
            tmp_path,
            "WOR-10",
            "epic/wor-253",
            "Implementation complete",
        )

    assert result == "deadbeef"
    assert any("squashing" in r.message for r in caplog.records)


def test_squash_wip_commits_no_op_when_no_wip_commits(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Returns None when no wip commits — no reset/commit/push calls."""
    sha = "a1b2c3d4e5f6"  # pragma: allowlist secret — fake SHA

    call_order: list[str] = []
    call_count = [0]

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        cmd = args[0] if args else kwargs.get("args", [])
        idx = call_count[0]
        call_count[0] = idx + 1
        if "merge-base" in cmd:
            call_order.append("merge-base")
            return _completed_process(stdout=sha + "\n")
        if "log" in cmd:
            call_order.append("log")
            # No wip commits — log is empty
            return _completed_process(stdout="")
        # Should not reach here
        call_order.append("unexpected")
        return _completed_process()

    with (
        patch(
            "app.core.watcher.watcher_worktrees.subprocess.run",
            side_effect=fake_run,
        ),
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_worktrees"),
    ):
        result = squash_wip_commits(
            tmp_path,
            "WOR-10",
            "epic/wor-253",
            "Implementation complete",
        )

    assert result is None
    assert call_order == ["merge-base", "log"]
    # No reset/commit/push calls — no-op
    assert "unexpected" not in call_order
    assert any("nothing to squash" in r.message for r in caplog.records)


def test_squash_wip_commits_returns_none_on_git_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any git failure returns None and logs a warning."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess:
        cmd = args[0] if args else kwargs.get("args", [])
        if "merge-base" in cmd:
            raise subprocess.CalledProcessError(1, "git", stderr="error")
        return _completed_process()

    with (
        patch(
            "app.core.watcher.watcher_worktrees.subprocess.run",
            side_effect=fake_run,
        ),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_worktrees"),
    ):
        result = squash_wip_commits(
            tmp_path,
            "WOR-10",
            "epic/wor-253",
            "Implementation complete",
        )

    assert result is None
    assert any("squash_wip_commits failed" in r.message for r in caplog.records)
