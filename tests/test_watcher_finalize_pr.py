"""Tests for finalize_worker — PR creation paths."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.watcher.watcher_types import ActiveWorker
from app.core.watcher.watcher_worktrees import WipPreservationResult
from tests._finalize_helpers import _call_finalize
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# PR creation failure marks ticket Blocked, no crash
# ---------------------------------------------------------------------------


def test_finalize_worker_pr_failure_marks_blocked(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        base_branch="main",
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    exc = subprocess.CalledProcessError(1, "gh", stderr="Head sha can't be blank")

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.create_pr", side_effect=exc),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    linear_mock.post_comment.assert_called_once()
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "WOR-10" in comment_body
    assert "Head sha can't be blank" in comment_body
    metrics_mock.record.assert_called_once()


# ---------------------------------------------------------------------------
# attempt_pr — direct (AC: success path returns success; error → failure)
# ---------------------------------------------------------------------------


def test_attempt_pr_success_returns_success(
    tmp_path: Path,
) -> None:
    """PR creation succeeds → 'success' returned."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    linear_mock = MagicMock()
    from app.core.watcher.watcher_finalize import attempt_pr

    with patch(
        "app.core.watcher.watcher_finalize.create_pr",
        return_value="https://github.com/example/pr/1",
    ):
        result = attempt_pr(manifest, worker, linear_mock)

    assert result[0] == "success"
    linear_mock.set_state.assert_not_called()
    linear_mock.post_comment.assert_not_called()


def test_attempt_pr_called_process_error_returns_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CalledProcessError → state set to failed, returns 'failure'."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    from app.core.watcher.watcher_finalize import attempt_pr

    exc = subprocess.CalledProcessError(1, "gh pr create", stderr="validation failed")
    with patch("app.core.watcher.watcher_finalize.create_pr", side_effect=exc):
        result = attempt_pr(manifest, worker, linear_mock)

    assert result[0] == "failure"
    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    linear_mock.post_comment.assert_called_once()
    comment_body = linear_mock.post_comment.call_args[0][1]
    assert "WOR-10" in comment_body
    assert "validation failed" in comment_body


# ---------------------------------------------------------------------------
# WOR-267 — attempt_pr calls commit_wip_state on failure
# ---------------------------------------------------------------------------


def test_attempt_pr_calls_commit_wip_state_on_failure(
    tmp_path: Path,
) -> None:
    """On CalledProcessError, commit_wip_state is called to preserve changes."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    from app.core.watcher.watcher_finalize import attempt_pr

    exc = subprocess.CalledProcessError(1, "gh pr create", stderr="validation failed")
    with (
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ) as mock_commit,
        patch("app.core.watcher.watcher_finalize.create_pr", side_effect=exc),
    ):
        result = attempt_pr(manifest, worker, linear_mock)

    assert result[0] == "failure"
    mock_commit.assert_called_once_with(
        tmp_path,
        "WOR-10",
        "wor-10-test-ticket",
    )
