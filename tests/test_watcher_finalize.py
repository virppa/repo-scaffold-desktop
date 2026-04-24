"""Tests for app.core.watcher._finalize_worker."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.linear_client import LinearError
from app.core.watcher import Watcher
from app.core.watcher_types import ActiveWorker
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# _finalize_worker — PR creation failure marks ticket Blocked, no crash
# ---------------------------------------------------------------------------


def test_finalize_worker_pr_failure_marks_blocked(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        base_branch="main",
    )
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    exc = subprocess.CalledProcessError(1, "gh", stderr="Head sha can't be blank")

    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.create_pr", side_effect=exc),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    linear_mock.post_comment.assert_called_once()
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "WOR-10" in comment_body
    assert "Head sha can't be blank" in comment_body
    metrics_mock.record.assert_called_once()


# ---------------------------------------------------------------------------
# retry_count wiring in _finalize_worker
# ---------------------------------------------------------------------------


def test_finalize_worker_retry_count_zero_on_success(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    call_kwargs = metrics_mock.record.call_args[0][0]
    assert call_kwargs.retry_count == 0


def test_finalize_worker_retry_count_increments_on_check_failure(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher.run_checks", return_value=False),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics"),
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    assert worker.retry_count == 2


def test_finalize_worker_retry_count_two_failures_then_success(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    check_results = [False, False, True]
    with (
        patch("app.core.watcher.run_checks", side_effect=check_results),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)
        w._finalize_worker(worker, returncode=0, wall_time=1.0)
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    call_kwargs = metrics_mock.record.call_args[0][0]
    assert call_kwargs.retry_count == 2


# ---------------------------------------------------------------------------
# _safe_set_state — daemon survives LinearError at finalize set_state sites
# ---------------------------------------------------------------------------


def test_finalize_worker_set_state_failure_nonzero_no_crash(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("rate limit")
    w = Watcher(linear_client=linear_mock)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics"),
    ):
        w._finalize_worker(worker, returncode=1, wall_time=1.0)


def test_finalize_worker_set_state_failure_success_path_no_crash(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("network error")
    w = Watcher(linear_client=linear_mock)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics"),
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)


# ---------------------------------------------------------------------------
# _finalize_worker — local_tokens + context_compactions wired from log
# ---------------------------------------------------------------------------


def test_finalize_worker_passes_usage_to_metrics(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-10.log"
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 2000, "output_tokens": 400},
                "context_compactions": 5,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_tokens == 2400
    assert m.context_compactions == 5


def test_finalize_worker_usage_none_when_no_log(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_tokens is None
    assert m.context_compactions is None


# ---------------------------------------------------------------------------
# _finalize_worker — sonar_findings_count wired to metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_sonar_count_wired_to_metrics(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch(
            "app.core.watcher.fetch_sonar_findings",
            return_value=["MAJOR", "MINOR", "MINOR"],
        ),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.sonar_findings_count == 3


def test_finalize_worker_sonar_count_none_when_unavailable(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch("app.core.watcher.fetch_sonar_findings", return_value=None),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.sonar_findings_count is None


# ---------------------------------------------------------------------------
# _finalize_worker — Sonar severity escalation classification
# ---------------------------------------------------------------------------


def _make_finalize_worker_with_empty_result(
    tmp_path: Path,
) -> tuple[Watcher, MagicMock, ActiveWorker]:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({"status": "success"}), encoding="utf-8")

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    return w, linear_mock, worker


def test_finalize_worker_sonar_blocker_escalates(tmp_path: Path) -> None:
    w, linear_mock, worker = _make_finalize_worker_with_empty_result(tmp_path)
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.preserve_worker_artifacts"),
        patch("app.core.watcher.fetch_sonar_findings", return_value=["BLOCKER"]),
        patch("app.core.watcher.create_pr") as mock_create_pr,
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"
    assert m.sonar_findings_count == 1


def test_finalize_worker_sonar_major_advisory_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w, _linear_mock, worker = _make_finalize_worker_with_empty_result(tmp_path)
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.preserve_worker_artifacts"),
        patch("app.core.watcher.fetch_sonar_findings", return_value=["MAJOR"]),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is False
    assert m.outcome == "success"
    assert m.sonar_findings_count == 1
    assert any("MAJOR" in msg and "fix_locally" in msg for msg in caplog.messages)


def test_finalize_worker_sonar_none_no_escalation(tmp_path: Path) -> None:
    w, _linear_mock, worker = _make_finalize_worker_with_empty_result(tmp_path)
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.preserve_worker_artifacts"),
        patch("app.core.watcher.fetch_sonar_findings", return_value=None),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is False
    assert m.outcome == "success"
    assert m.sonar_findings_count is None


# ---------------------------------------------------------------------------
# _finalize_worker — EscalationPolicy flag routing
# ---------------------------------------------------------------------------


def _make_finalize_worker_for_policy(
    tmp_path: Path,
    flags: dict[str, bool],
) -> tuple[Watcher, MagicMock, ActiveWorker]:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({"status": "success", **flags}), encoding="utf-8")

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    return w, linear_mock, worker


def test_finalize_worker_scope_drift_escalates(tmp_path: Path) -> None:
    w, linear_mock, worker = _make_finalize_worker_for_policy(
        tmp_path, {"scope_drift": True}
    )
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.preserve_worker_artifacts"),
        patch("app.core.watcher.create_pr") as mock_create_pr,
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "scope_drift" in comment_body
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"


def test_finalize_worker_forbidden_path_touched_escalates(tmp_path: Path) -> None:
    w, linear_mock, worker = _make_finalize_worker_for_policy(
        tmp_path, {"forbidden_path_touched": True}
    )
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.preserve_worker_artifacts"),
        patch("app.core.watcher.create_pr") as mock_create_pr,
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "forbidden_path_touched" in comment_body
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"


def test_finalize_worker_no_flags_proceeds_normally(tmp_path: Path) -> None:
    w, _linear_mock, worker = _make_finalize_worker_for_policy(tmp_path, {})
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False


def test_finalize_worker_missing_result_json_proceeds_normally(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher.run_checks", return_value=True),
        patch("app.core.watcher.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.create_pr", return_value="https://github.com/example/pr/1"
        ),
        patch("app.core.watcher.cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False
