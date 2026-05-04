"""WIP-preservation, result.json, and cleanup-gating tests for finalize_worker.

Extracted from test_watcher_finalize.py for the file-size gate (WOR-282).
Covers WOR-258 (commit_wip_state integration), WOR-286 (trust result.json over
non-zero exit code), and WOR-288 (gate cleanup_worktree on wip preservation).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
from app.core.manifest import FailurePolicy
from app.core.watcher.watcher_finalize import finalize_worker
from app.core.watcher.watcher_types import ActiveWorker
from app.core.watcher.watcher_worktrees import WipPreservationResult
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEFAULT_PROJECT = "repo-scaffold-desktop"


def _call_finalize(
    worker: ActiveWorker,
    *,
    returncode: int = 0,
    wall_time: float = 1.0,
    linear: object | None = None,
    metrics: object | None = None,
    repo_root: Path | None = None,
    mode: str = "default",
) -> None:
    finalize_worker(
        worker,
        returncode=returncode,
        wall_time=wall_time,
        linear=linear or MagicMock(),  # type: ignore[arg-type]
        metrics=metrics or MagicMock(),  # type: ignore[arg-type]
        escalation_policy=EscalationPolicy.from_toml(),
        repo_root=repo_root or Path("."),
        mode=mode,
        project_id=_DEFAULT_PROJECT,
    )


# ---------------------------------------------------------------------------
# WOR-258 — commit_wip_state integration
# ---------------------------------------------------------------------------


def test_finalize_worker_calls_commit_wip_state_on_check_failure(
    tmp_path: Path,
) -> None:
    """commit_wip_state is called when checks fail with abort policy."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="pushed", sha="a1b2c3d4", backup_path=None, error=None
            ),
        ) as mock_commit,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    # WOR-288: finalize_worker now passes a backup_root kwarg to commit_wip_state.
    # Assert the positional args + keyword presence rather than exact call match.
    mock_commit.assert_called_once()
    args, kwargs = mock_commit.call_args
    assert args == (tmp_path, "WOR-10", "wor-10-test-ticket")
    assert "backup_root" in kwargs


def test_finalize_worker_writes_last_failure_json_on_wip_commit(
    tmp_path: Path,
) -> None:
    """last_failure.json in the worktree is updated with wip_commit_sha."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    # Create an existing last_failure.json in the worktree
    artifact_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    failure_file = artifact_dir / "last_failure.json"
    failure_file.write_text('{"failed_at": "2026-01-01"}', encoding="utf-8")

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="pushed", sha="a1b2c3d4", backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    data = json.loads(failure_file.read_text(encoding="utf-8"))
    assert data["failed_at"] == "2026-01-01"
    assert data["wip_commit_sha"] == "a1b2c3d4"


def test_finalize_worker_last_failure_json_created_when_absent(
    tmp_path: Path,
) -> None:
    """last_failure.json is created if it does not exist yet."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    artifact_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # No last_failure.json exists

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="pushed", sha="a1b2c3d4", backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    failure_file = artifact_dir / "last_failure.json"
    assert failure_file.exists()
    data = json.loads(failure_file.read_text(encoding="utf-8"))
    assert data["wip_commit_sha"] == "a1b2c3d4"


def test_finalize_worker_skips_commit_wip_when_no_sha(tmp_path: Path) -> None:
    """When commit_wip_state returns None, no last_failure.json update."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="failed", sha=None, backup_path=None, error="boom"
            ),
        ) as mock_commit,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree") as mock_cleanup,
    ):
        _call_finalize(worker, metrics=metrics_mock)

    mock_commit.assert_called_once()
    # No last_failure.json should be created or modified
    artifact_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    failure_file = artifact_dir / "last_failure.json"
    assert not failure_file.exists()
    # WOR-288: when WIP preservation fails, the worktree must NOT be removed.
    mock_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# WOR-286 — trust result.json status when worker exit code disagrees
# ---------------------------------------------------------------------------


def _write_result_json(repo_root: Path, ticket_id: str, status: str) -> Path:
    """Write a minimal worker result.json under repo_root's artifact dir."""
    art = repo_root / ".claude" / "artifacts" / ticket_id.lower().replace("-", "_")
    art.mkdir(parents=True, exist_ok=True)
    f = art / "result.json"
    f.write_text(
        json.dumps({"ticket_id": ticket_id, "status": status, "summary": "x"}),
        encoding="utf-8",
    )
    return f


def test_finalize_worker_success_resultjson_overrides_nonzero_exit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Worker exits non-zero but result.json says success — checks run, PR created."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_result_json(repo_root, "WOR-10", "success")
    metrics_mock = MagicMock()
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ) as mock_create_pr,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"),
    ):
        _call_finalize(
            worker,
            returncode=1,  # NON-ZERO exit — but result.json says success
            linear=linear_mock,
            metrics=metrics_mock,
            repo_root=repo_root,
        )

    # PR was created despite the non-zero exit
    mock_create_pr.assert_called_once()
    # WARNING about exit-code disagreement was logged
    assert any(
        "exited non-zero" in r.message
        and "result.json reports status=success" in r.message
        for r in caplog.records
    )
    # Outcome recorded as success in metrics
    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"


def test_finalize_worker_failure_resultjson_with_nonzero_exit_routes_failure(
    tmp_path: Path,
) -> None:
    """Worker exits non-zero AND result.json says failure → existing failure path."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_result_json(repo_root, "WOR-10", "failure")
    metrics_mock = MagicMock()
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker,
            returncode=1,
            linear=linear_mock,
            metrics=metrics_mock,
            repo_root=repo_root,
        )

    # Linear set to failed-state ('Blocked' from the default ticket_state_map)
    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    # Outcome recorded as failure
    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "failure"


def test_finalize_worker_missing_resultjson_with_nonzero_exit_routes_failure(
    tmp_path: Path,
) -> None:
    """Worker exits non-zero AND no result.json → failure path (no in-band signal)."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    # NB: no result.json written
    metrics_mock = MagicMock()
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker,
            returncode=1,
            linear=linear_mock,
            metrics=metrics_mock,
            repo_root=tmp_path,
        )

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "failure"


def test_finalize_worker_success_resultjson_but_checks_fail_routes_failure(
    tmp_path: Path,
) -> None:
    """result.json says success but a required check fails → still routes to failure."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_result_json(repo_root, "WOR-10", "success")
    metrics_mock = MagicMock()
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker,
            returncode=1,  # success result.json + non-zero exit — checks decide
            linear=linear_mock,
            metrics=metrics_mock,
            repo_root=repo_root,
        )

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "failure"


# ---------------------------------------------------------------------------
# WOR-288 — cleanup_worktree gated on commit_wip_state result
# ---------------------------------------------------------------------------


def test_finalize_worker_failure_path_cleanup_skipped_when_wip_failed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """commit_wip_state status='failed' → cleanup_worktree NOT called, ERROR logged."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="failed",
                sha=None,
                backup_path=None,
                error="pre-commit hook rejected commit",
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree") as mock_cleanup,
        caplog.at_level(logging.ERROR, logger="app.core.watcher.watcher_finalize"),
    ):
        _call_finalize(worker, metrics=metrics_mock, repo_root=tmp_path)

    mock_cleanup.assert_not_called()
    assert any(
        "WIP preservation failed for WOR-10" in r.message
        and "leaving worktree" in r.message
        for r in caplog.records
    )


def test_finalize_worker_failure_path_cleanup_runs_when_wip_pushed(
    tmp_path: Path,
) -> None:
    """commit_wip_state status='pushed' → cleanup_worktree IS called."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="pushed", sha="a1b2c3d4", backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree") as mock_cleanup,
    ):
        _call_finalize(worker, metrics=metrics_mock, repo_root=tmp_path)

    mock_cleanup.assert_called_once()


def test_finalize_worker_failure_path_cleanup_runs_when_wip_backup(
    tmp_path: Path,
) -> None:
    """commit_wip_state status='backup' → cleanup IS called (work in backup)."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="backup",
                sha=None,
                backup_path=tmp_path / "backup",
                error="push rejected",
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree") as mock_cleanup,
    ):
        _call_finalize(worker, metrics=metrics_mock, repo_root=tmp_path)

    mock_cleanup.assert_called_once()


def test_finalize_worker_failure_path_cleanup_runs_when_wip_clean(
    tmp_path: Path,
) -> None:
    """commit_wip_state status='clean' (nothing to preserve) → cleanup IS called."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree") as mock_cleanup,
    ):
        _call_finalize(worker, metrics=metrics_mock, repo_root=tmp_path)

    mock_cleanup.assert_called_once()


def test_finalize_worker_passes_backup_root_to_commit_wip_state(
    tmp_path: Path,
) -> None:
    """finalize_worker passes backup_root=<repo>/.claude/artifacts to wip preserve."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path / "worktree",
        process=MagicMock(spec=subprocess.Popen),
    )

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ) as mock_commit,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock, repo_root=repo_root)

    _, kwargs = mock_commit.call_args
    assert kwargs.get("backup_root") == repo_root / ".claude" / "artifacts"
