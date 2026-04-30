"""Tests for app.core.watcher_finalize — free finalization functions."""

from __future__ import annotations

import json
import logging
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import LinearError
from app.core.manifest import FailurePolicy
from app.core.metrics import ReworkEvent
from app.core.watcher_finalize import (
    _infer_category,
    _read_result_data,
    _try_post_comment,
    finalize_worker,
    write_improvement_log_finding,
)
from app.core.watcher_types import ActiveWorker
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
) -> None:
    with patch(
        "app.core.watcher_finalize.write_improvement_log_finding",
    ):
        finalize_worker(
            worker,
            returncode=returncode,
            wall_time=wall_time,
            linear=linear or MagicMock(),
            metrics=metrics or MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=repo_root or Path("."),
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )


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
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.create_pr", side_effect=exc),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    linear_mock.post_comment.assert_called_once()
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "WOR-10" in comment_body
    assert "Head sha can't be blank" in comment_body
    metrics_mock.record.assert_called_once()


# ---------------------------------------------------------------------------
# retry_count wiring
# ---------------------------------------------------------------------------


def test_finalize_worker_retry_count_zero_on_success(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    call_kwargs = metrics_mock.record.call_args[0][0]
    assert call_kwargs.retry_count == 0


def test_finalize_worker_retry_count_increments_on_check_failure(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=False),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker)
        _call_finalize(worker)

    assert worker.retry_count == 2


def test_finalize_worker_retry_count_two_failures_then_success(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    check_results = [False, False, True]
    with (
        patch("app.core.watcher_finalize.run_checks", side_effect=check_results),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)
        _call_finalize(worker, metrics=metrics_mock)
        _call_finalize(worker, metrics=metrics_mock)

    call_kwargs = metrics_mock.record.call_args[0][0]
    assert call_kwargs.retry_count == 2


# ---------------------------------------------------------------------------
# safe_set_state — daemon survives LinearError at finalize set_state sites
# ---------------------------------------------------------------------------


def test_finalize_worker_set_state_failure_nonzero_no_crash(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("rate limit")

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with patch("app.core.watcher_finalize.cleanup_worktree"):
        _call_finalize(worker, returncode=1, linear=linear_mock)


def test_finalize_worker_set_state_failure_success_path_no_crash(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("network error")

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock)


# ---------------------------------------------------------------------------
# local_tokens + context_compactions wired from log
# ---------------------------------------------------------------------------


def test_finalize_worker_passes_usage_to_metrics(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()

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
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_tokens == 2400
    assert m.context_compactions == 5


def test_finalize_worker_usage_none_when_no_log(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_tokens is None
    assert m.context_compactions is None


# ---------------------------------------------------------------------------
# sonar_findings_count wired to metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_sonar_count_wired_to_metrics(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher_finalize.fetch_sonar_findings",
            return_value=["MAJOR", "MINOR", "MINOR"],
        ),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.sonar_findings_count == 3


def test_finalize_worker_sonar_count_none_when_unavailable(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
        patch("app.core.watcher_finalize.fetch_sonar_findings", return_value=None),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.sonar_findings_count is None


# ---------------------------------------------------------------------------
# Sonar severity escalation classification
# ---------------------------------------------------------------------------


def _make_worker_with_result(
    tmp_path: Path, flags: dict[str, bool]
) -> tuple[MagicMock, ActiveWorker]:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()

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
    return linear_mock, worker


def test_finalize_worker_sonar_blocker_escalates(tmp_path: Path) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher_finalize.fetch_sonar_findings", return_value=["BLOCKER"]
        ),
        patch("app.core.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"
    assert m.sonar_findings_count == 1


def test_finalize_worker_sonar_major_advisory_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher_finalize.fetch_sonar_findings", return_value=["MAJOR"]),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
        caplog.at_level(logging.WARNING, logger="app.core.watcher_finalize"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is False
    assert m.outcome == "success"
    assert m.sonar_findings_count == 1
    assert any("MAJOR" in msg and "fix_locally" in msg for msg in caplog.messages)


def test_finalize_worker_sonar_none_no_escalation(tmp_path: Path) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher_finalize.fetch_sonar_findings", return_value=None),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is False
    assert m.outcome == "success"
    assert m.sonar_findings_count is None


# ---------------------------------------------------------------------------
# EscalationPolicy flag routing
# ---------------------------------------------------------------------------


def test_finalize_worker_scope_drift_escalates(tmp_path: Path) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {"scope_drift": True})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "scope_drift" in comment_body
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"


def test_finalize_worker_forbidden_path_touched_escalates(tmp_path: Path) -> None:
    linear_mock, worker = _make_worker_with_result(
        tmp_path, {"forbidden_path_touched": True}
    )
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "forbidden_path_touched" in comment_body
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"


def test_finalize_worker_no_flags_proceeds_normally(tmp_path: Path) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False


def test_finalize_worker_missing_result_json_proceeds_normally(tmp_path: Path) -> None:
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False


# ---------------------------------------------------------------------------
# Human policy action — _handle_policy_outcome 'human' branch (lines 201-209)
# ---------------------------------------------------------------------------


def test_finalize_worker_human_policy_posts_comment_and_aborts(
    tmp_path: Path,
) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher_finalize.cleanup_worktree"),
        patch.object(EscalationPolicy, "classify_result", return_value="human"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_not_called()
    linear_mock.post_comment.assert_called_once()
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "Human review required" in comment_body
    assert "WOR-10" in comment_body
    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "aborted"
    assert m.escalated_to_cloud is False


# ---------------------------------------------------------------------------
# _try_post_comment exception guard (lines 257-258)
# ---------------------------------------------------------------------------


def test_try_post_comment_swallows_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    linear_mock = MagicMock()
    linear_mock.post_comment.side_effect = Exception("connection reset by peer")

    with caplog.at_level(logging.WARNING, logger="app.core.watcher_finalize"):
        _try_post_comment(linear_mock, "lin-id", "WOR-10", "some comment body")

    assert any("Could not post comment" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# escalate_to_cloud branching in _execute_finalization
# ---------------------------------------------------------------------------


def test_execute_finalization_check_failure_escalates_to_cloud(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort", escalate_to_cloud=True),
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
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=False),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    linear_mock.post_comment.assert_called_once()
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True


def test_execute_finalization_check_failure_blocked_when_no_escalate(
    tmp_path: Path,
) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
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
    with (
        patch("app.core.watcher_finalize.run_checks", return_value=False),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is False


def test_execute_finalization_nonzero_exit_escalates_to_cloud(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(escalate_to_cloud=True),
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
    with patch("app.core.watcher_finalize.cleanup_worktree"):
        _call_finalize(worker, returncode=1, linear=linear_mock, metrics=metrics_mock)

    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    linear_mock.post_comment.assert_called_once()
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True


# ---------------------------------------------------------------------------
# _execute_finalization — explicit return-value assertions (AC)
# ---------------------------------------------------------------------------


def test_execute_finalization_nonzero_returncode_returns_failure(
    tmp_path: Path,
) -> None:
    """non-zero returncode → 'failure' returned (not just logged)."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    from app.core.watcher_finalize import _execute_finalization

    result = _execute_finalization(
        worker, 1, linear_mock, EscalationPolicy.from_toml(), tmp_path, MagicMock()
    )
    outcome, escalated, preserved, findings, _result_data = result

    assert outcome == "failure"
    assert escalated is False
    assert preserved is False
    assert findings is None


def test_execute_finalization_check_failure_abort_returns_failure(
    tmp_path: Path,
) -> None:
    """checks fail with on_check_failure='abort' → 'failure' returned."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(on_check_failure="abort"),
    )
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    from app.core.watcher_finalize import _execute_finalization

    with patch("app.core.watcher_finalize.run_checks", return_value=False):
        result = _execute_finalization(
            worker, 0, linear_mock, EscalationPolicy.from_toml(), tmp_path, MagicMock()
        )
    outcome, escalated, preserved, findings, _result_data = result

    assert outcome == "failure"
    assert escalated is False
    assert preserved is False
    assert findings is None


# ---------------------------------------------------------------------------
# _handle_policy_outcome — explicit return-value assertions (AC)
# ---------------------------------------------------------------------------


def test_handle_policy_outcome_escalate_returns_escalated(
    tmp_path: Path,
) -> None:
    """action='escalate' → returns 'escalated'."""
    linear_mock = MagicMock()
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    from app.core.watcher_finalize import _handle_policy_outcome

    outcome, escalated, findings = _handle_policy_outcome(
        "escalate",
        {"scope_drift": True},
        worker,
        linear_mock,
        EscalationPolicy.from_toml(),
        MagicMock(),
    )

    assert outcome == "escalated"
    assert escalated is True
    assert findings is None


def test_handle_policy_outcome_human_returns_aborted(tmp_path: Path) -> None:
    """action='human' → returns 'aborted'."""
    linear_mock = MagicMock()
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    from app.core.watcher_finalize import _handle_policy_outcome

    outcome, escalated, findings = _handle_policy_outcome(
        "human",
        {"scope_drift": True},
        worker,
        linear_mock,
        EscalationPolicy.from_toml(),
        MagicMock(),
    )

    assert outcome == "aborted"
    assert escalated is False
    assert findings is None


# ---------------------------------------------------------------------------
# _sonar_requires_escalation — boundary cases (AC)
# ---------------------------------------------------------------------------


def test_sonar_requires_escalation_empty_list(tmp_path: Path) -> None:
    """returns False for empty findings list."""
    linear_mock = MagicMock()
    from app.core.watcher_finalize import _sonar_requires_escalation

    assert (
        _sonar_requires_escalation(
            [], "WOR-10", "fake-id", linear_mock, EscalationPolicy.from_toml()
        )
        is False
    )


def test_sonar_requires_escalation_severity_triggers_true() -> None:
    """returns True when escalation_policy maps severity to 'escalate'."""
    linear_mock = MagicMock()
    from app.core.watcher_finalize import _sonar_requires_escalation

    # Default policy: BLOCKER → escalate
    assert (
        _sonar_requires_escalation(
            ["BLOCKER"], "WOR-10", "fake-id", linear_mock, EscalationPolicy.from_toml()
        )
        is True
    )
    assert (
        _sonar_requires_escalation(
            ["CRITICAL"], "WOR-10", "fake-id", linear_mock, EscalationPolicy.from_toml()
        )
        is True
    )


def test_sonar_requires_escalation_no_triggers_false() -> None:
    """returns False when no severity maps to 'escalate'."""
    linear_mock = MagicMock()
    from app.core.watcher_finalize import _sonar_requires_escalation

    # Default policy: MAJOR, MINOR, INFO → fix_locally (not escalate)
    assert (
        _sonar_requires_escalation(
            ["MAJOR", "MINOR", "INFO"],
            "WOR-10",
            "fake-id",
            linear_mock,
            EscalationPolicy.from_toml(),
        )
        is False
    )


# ---------------------------------------------------------------------------
# safe_set_state — direct (AC: LinearError caught and logged as warning)
# ---------------------------------------------------------------------------


def test_safe_set_state_linear_error_logged_as_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LinearError is caught and logged as warning (does not raise)."""
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("network timeout")

    with caplog.at_level(logging.WARNING, logger="app.core.watcher_finalize"):
        # Should NOT raise — catches LinearError internally
        from app.core.watcher_finalize import safe_set_state

        safe_set_state(linear_mock, "fake-linear-id", "Blocked", "WOR-10")

    # set_state was called but the exception was caught and not re-raised
    linear_mock.set_state.assert_called_once_with("fake-linear-id", "Blocked")
    assert any("set_state failed" in msg for msg in caplog.messages)


def test_safe_set_state_success_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Successful set_state produces no warning log."""
    linear_mock = MagicMock()
    with caplog.at_level(logging.WARNING, logger="app.core.watcher_finalize"):
        from app.core.watcher_finalize import safe_set_state

        safe_set_state(linear_mock, "fake-linear-id", "In Progress", "WOR-10")

    assert not caplog.text or "set_state failed" not in caplog.text
    linear_mock.set_state.assert_called_once_with("fake-linear-id", "In Progress")


# ---------------------------------------------------------------------------
# attempt_pr — direct (AC: success path returns 'success'; error → 'failure')
# ---------------------------------------------------------------------------


def test_attempt_pr_success_returns_success(
    tmp_path: Path,
) -> None:
    """PR creation succeeds → 'success' returned."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    linear_mock = MagicMock()
    from app.core.watcher_finalize import attempt_pr

    with patch(
        "app.core.watcher_finalize.create_pr",
        return_value="https://github.com/example/pr/1",
    ):
        result = attempt_pr(manifest, worker, linear_mock)

    assert result == "success"
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
    from app.core.watcher_finalize import attempt_pr

    exc = subprocess.CalledProcessError(1, "gh pr create", stderr="validation failed")
    with patch("app.core.watcher_finalize.create_pr", side_effect=exc):
        result = attempt_pr(manifest, worker, linear_mock)

    assert result == "failure"
    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    linear_mock.post_comment.assert_called_once()
    comment_body = linear_mock.post_comment.call_args[0][1]
    assert "WOR-10" in comment_body
    assert "validation failed" in comment_body


# ---------------------------------------------------------------------------
# WOR-230 — local_input_tokens / local_output_tokens wired to metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_writes_separate_token_fields(tmp_path: Path) -> None:
    """input_tokens and output_tokens are passed to TicketMetrics."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-10.log"
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 15000, "output_tokens": 600},
                "context_compactions": 2,
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
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, wall_time=10.0, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_input_tokens == 15000
    assert m.local_output_tokens == 600
    assert m.local_tokens == 15600  # backward-compat sum
    assert m.local_output_tokens_per_second == pytest.approx(60.0)  # 600/10


def test_finalize_worker_token_fields_none_when_no_log(
    tmp_path: Path,
) -> None:
    """When log is missing, all new token fields are None."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_input_tokens is None
    assert m.local_output_tokens is None
    assert m.local_tokens is None
    assert m.local_output_tokens_per_second is None


# ---------------------------------------------------------------------------
# Improvement log — _read_result_data
# ---------------------------------------------------------------------------


def test_read_result_data_returns_dict_on_valid_json(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "ticket_id": "WOR-10",
                "status": "success",
                "notes": "fixed lint",
                "checks_failed": [],
                "scope_drift": False,
                "forbidden_path_touched": False,
                "escalated_to_cloud": False,
                "summary": "Fixed imports",
            }
        ),
        encoding="utf-8",
    )

    result = _read_result_data(result_path)

    assert result is not None
    assert result["ticket_id"] == "WOR-10"
    assert result["status"] == "success"
    assert result["notes"] == "fixed lint"


def test_read_result_data_missing_file(tmp_path: Path) -> None:
    assert _read_result_data(tmp_path / "nonexistent.json") is None


def test_read_result_data_malformed_json(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text("not json", encoding="utf-8")
    assert _read_result_data(result_path) is None


def test_read_result_data_returns_only_selected_keys(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(
        json.dumps({"extra_key": "value", "ticket_id": "WOR-99"}),
        encoding="utf-8",
    )
    result = _read_result_data(result_path)
    assert result is not None
    assert "extra_key" not in result
    assert result["ticket_id"] == "WOR-99"


# ---------------------------------------------------------------------------
# Improvement log — _infer_category
# ---------------------------------------------------------------------------


def test_infer_category_scope_drift(tmp_path: Path) -> None:
    assert (
        _infer_category(
            scope_drift=True,
            forbidden_path_touched=False,
            check_failures=[],
            wall_time=0,
            runtime_threshold_minutes=60,
            escalated=False,
            notes="",
            status="success",
        )
        == "scope"
    )


def test_infer_category_forbidden_path(tmp_path: Path) -> None:
    assert (
        _infer_category(
            scope_drift=False,
            forbidden_path_touched=True,
            check_failures=[],
            wall_time=0,
            runtime_threshold_minutes=60,
            escalated=False,
            notes="",
            status="success",
        )
        == "scope"
    )


def test_infer_category_quality(tmp_path: Path) -> None:
    assert (
        _infer_category(
            scope_drift=False,
            forbidden_path_touched=False,
            check_failures=["ruff check"],
            wall_time=0,
            runtime_threshold_minutes=60,
            escalated=False,
            notes="",
            status="success",
        )
        == "quality"
    )


def test_infer_category_perf(tmp_path: Path) -> None:
    assert (
        _infer_category(
            scope_drift=False,
            forbidden_path_touched=False,
            check_failures=[],
            wall_time=3700,  # > 60 min
            runtime_threshold_minutes=60,
            escalated=False,
            notes="",
            status="success",
        )
        == "perf"
    )


def test_infer_category_escalation(tmp_path: Path) -> None:
    assert (
        _infer_category(
            scope_drift=False,
            forbidden_path_touched=False,
            check_failures=[],
            wall_time=120,
            runtime_threshold_minutes=60,
            escalated=True,
            notes="failed after retries",
            status="success",
        )
        == "escalation"
    )


def test_infer_category_improvement_success(tmp_path: Path) -> None:
    assert (
        _infer_category(
            scope_drift=False,
            forbidden_path_touched=False,
            check_failures=[],
            wall_time=300,
            runtime_threshold_minutes=60,
            escalated=False,
            notes="clean implementation",
            status="success",
        )
        == "improvement"
    )


def test_infer_category_scope_wins_over_quality(tmp_path: Path) -> None:
    assert (
        _infer_category(
            scope_drift=True,
            forbidden_path_touched=False,
            check_failures=["ruff check"],
            wall_time=0,
            runtime_threshold_minutes=60,
            escalated=False,
            notes="",
            status="success",
        )
        == "scope"
    )


# ---------------------------------------------------------------------------
# Improvement log — write_improvement_log_finding
# ---------------------------------------------------------------------------


def _make_config(ticket_id: str = "WOR-254") -> "ImprovementLogConfig":  # noqa: F821
    """Helper to create an ImprovementLogConfig for tests."""
    from app.core.escalation_policy import ImprovementLogConfig

    return ImprovementLogConfig(ticket_id=ticket_id)


def test_write_improvement_log_posts_comment_on_success(tmp_path: Path) -> None:
    from app.core.escalation_policy import ImprovementLogConfig

    linear_mock = MagicMock()
    linear_mock.list_comments.return_value = [{"body": "a"}]  # 1 comment

    config = ImprovementLogConfig(ticket_id="WOR-254", review_threshold=15)
    write_improvement_log_finding(
        linear=linear_mock,
        linear_id="fake-linear-id",
        improvement_log_config=config,
        result_data={
            "ticket_id": "WOR-10",
            "epic_id": "WOR-96",
            "status": "success",
            "notes": "fixed imports",
            "checks_failed": [],
            "scope_drift": False,
            "forbidden_path_touched": False,
            "escalated_to_cloud": False,
        },
        ticket_id="WOR-10",
        epic_id="WOR-96",
        wall_time=300,
    )

    linear_mock.post_comment.assert_called_once()
    body = linear_mock.post_comment.call_args[0][1]
    assert "WOR-10" in body
    assert "WOR-96" in body
    assert "improvement:" in body


def test_write_improvement_log_skipped_when_config_none(tmp_path: Path) -> None:
    linear_mock = MagicMock()

    write_improvement_log_finding(
        linear=linear_mock,
        linear_id="fake-linear-id",
        improvement_log_config=None,  # type: ignore[arg-type]
        result_data={},
        ticket_id="WOR-10",
        epic_id="WOR-96",
        wall_time=300,
    )

    linear_mock.post_comment.assert_not_called()


def test_write_improvement_log_sets_ready_for_review_when_above_threshold(
    tmp_path: Path,
) -> None:
    from app.core.escalation_policy import ImprovementLogConfig

    linear_mock = MagicMock()
    comments = [{"body": "x"} for _ in range(16)]  # 16 > threshold
    linear_mock.list_comments.return_value = comments

    config = ImprovementLogConfig(ticket_id="WOR-254", review_threshold=15)
    write_improvement_log_finding(
        linear=linear_mock,
        linear_id="fake-linear-id",
        improvement_log_config=config,
        result_data={
            "ticket_id": "WOR-10",
            "epic_id": "WOR-96",
            "status": "success",
            "notes": "fixed imports",
            "checks_failed": [],
            "scope_drift": False,
            "forbidden_path_touched": False,
            "escalated_to_cloud": False,
        },
        ticket_id="WOR-10",
        epic_id="WOR-96",
        wall_time=300,
    )

    linear_mock.set_state.assert_called_once_with("WOR-254", "ReadyForReview")


def test_write_improvement_log_comment_format(tmp_path: Path) -> None:
    from app.core.escalation_policy import ImprovementLogConfig

    linear_mock = MagicMock()
    linear_mock.list_comments.return_value = [{"body": "a"}]

    config = ImprovementLogConfig(ticket_id="WOR-254")
    write_improvement_log_finding(
        linear=linear_mock,
        linear_id="fake-linear-id",
        improvement_log_config=config,
        result_data={
            "ticket_id": "WOR-10",
            "epic_id": "WOR-96",
            "status": "success",
            "notes": "clean implementation",
            "checks_failed": [],
            "scope_drift": False,
            "forbidden_path_touched": False,
            "escalated_to_cloud": False,
        },
        ticket_id="WOR-10",
        epic_id="WOR-96",
        wall_time=300,
    )

    call_args = linear_mock.post_comment.call_args[0]
    assert call_args[0] == "WOR-254"  # issue_id
    body = call_args[1]
    # Format: [{ticket_id} / epic {epic_id} / {runtime_min}min]
    #         {category}: {one sentence finding}
    assert "[WOR-10 / epic WOR-96 / 5min]" in body
    assert "improvement:" in body


def test_write_improvement_log_no_crash_on_linear_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """write_improvement_log_finding catches Linear errors and logs warning."""
    from app.core.escalation_policy import ImprovementLogConfig

    linear_mock = MagicMock()
    linear_mock.list_comments.side_effect = Exception("connection reset")

    config = ImprovementLogConfig(ticket_id="WOR-254")
    write_improvement_log_finding(
        linear=linear_mock,
        linear_id="fake-linear-id",
        improvement_log_config=config,
        result_data={
            "ticket_id": "WOR-10",
            "epic_id": "WOR-96",
            "status": "success",
            "notes": "ok",
            "checks_failed": [],
            "scope_drift": False,
            "forbidden_path_touched": False,
            "escalated_to_cloud": False,
        },
        ticket_id="WOR-10",
        epic_id="WOR-96",
        wall_time=300,
    )

    assert any("Could not fetch comment count" in msg for msg in caplog.messages)


def test_write_improvement_log_with_check_failures(tmp_path: Path) -> None:
    """Finding uses check_failures for one-sentence when present."""
    from app.core.escalation_policy import ImprovementLogConfig

    linear_mock = MagicMock()
    linear_mock.list_comments.return_value = [{"body": "a"}]

    config = ImprovementLogConfig(ticket_id="WOR-254")
    write_improvement_log_finding(
        linear=linear_mock,
        linear_id="fake-linear-id",
        improvement_log_config=config,
        result_data={
            "ticket_id": "WOR-10",
            "epic_id": "WOR-96",
            "status": "success",
            "notes": "some notes",
            "checks_failed": ["ruff check .", "mypy app/"],
            "scope_drift": False,
            "forbidden_path_touched": False,
            "escalated_to_cloud": False,
        },
        ticket_id="WOR-10",
        epic_id="WOR-96",
        wall_time=300,
    )

    body = linear_mock.post_comment.call_args[0][1]
    assert "2 check(s) failed" in body
    assert "ruff check ." in body


# ---------------------------------------------------------------------------
# Integration: finalize_worker with improvement_log config
# ---------------------------------------------------------------------------


def test_finalize_worker_posts_improvement_log_finding_when_configured(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When improvement_log is configured, finalize_worker posts a finding."""
    from app.core.escalation_policy import ImprovementLogConfig

    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    linear_mock = MagicMock()
    linear_mock.list_comments.return_value = [{"body": "a"}]  # 1 comment

    metrics_mock = MagicMock()

    # Build a policy with improvement_log configured
    policy = EscalationPolicy.from_toml()
    config = ImprovementLogConfig(
        ticket_id="WOR-254",
        review_threshold=15,
    )
    policy.improvement_log = config

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    # Create a minimal result.json
    result_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "ticket_id": "WOR-10",
                "epic_id": "WOR-96",
                "status": "success",
                "notes": "clean implementation",
                "checks_failed": [],
                "scope_drift": False,
                "forbidden_path_touched": False,
                "escalated_to_cloud": False,
                "summary": "Fixed imports",
            }
        ),
        encoding="utf-8",
    )

    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=300,
            linear=linear_mock,
            metrics=metrics_mock,
            escalation_policy=policy,
            repo_root=tmp_path,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    # Verify the finding comment was posted to the improvement log ticket
    post_comment_calls = [
        c for c in linear_mock.post_comment.call_args_list if c[0][0] == "WOR-254"
    ]
    assert len(post_comment_calls) == 1
    assert "WOR-10" in post_comment_calls[0][0][1]


def test_finalize_worker_no_crash_without_improvement_log_config(
    tmp_path: Path,
) -> None:
    """When improvement_log is None, finalize_worker proceeds normally."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    # Use minimal TOML (no improvement_log section)
    minimal_toml = textwrap.dedent(
        """
        [retry]
        max_consecutive_failures = 3

        [auto_escalate]
        scope_drift = "escalate"
        forbidden_path_touched = "escalate"
        import_linter_violation = "escalate"
        security_blocker = "escalate"

        [human_escalate]
        architecture_change = "human"
        schema_migration = "human"
        cross_module_refactor = "human"
        auth_payments_touched = "human"

        [sonar]
        blocker = "escalate"
        critical = "escalate"
        major = "fix_locally"
        minor = "fix_locally"
        info = "fix_locally"
        """,
    )
    policy_path = tmp_path / "escalation_policy.toml"
    policy_path.write_text(minimal_toml, encoding="utf-8")
    policy = EscalationPolicy.from_toml(policy_path)
    assert policy.improvement_log is None

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher_finalize.run_checks", return_value=True),
        patch(
            "app.core.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher_finalize.cleanup_worktree"),
        patch("app.core.watcher_finalize.preserve_worker_artifacts"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=300,
            linear=linear_mock,
            metrics=metrics_mock,
            escalation_policy=policy,
            repo_root=tmp_path,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    # No call to post_comment on the improvement log ticket
    assert not any(
        c[0][0] == "WOR-254" for c in linear_mock.post_comment.call_args_list
    )
    # Normal operations still happen
    linear_mock.set_state.assert_not_called()


# ---------------------------------------------------------------------------
# Rework event recording (WOR-212)
# ---------------------------------------------------------------------------


class TestReworkEventRecording:
    """Tests that rework_events rows are written by _execute_finalization."""

    def test_rework_on_check_failure_local_retry(self, tmp_path: Path) -> None:
        """Check failure records rework_event with reason=local_retry."""
        manifest = make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
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
            patch("app.core.watcher_finalize.run_checks", return_value=False),
            patch("app.core.watcher_finalize.cleanup_worktree"),
        ):
            _call_finalize(worker, metrics=metrics_mock)

        calls = metrics_mock.record_rework_event.call_args_list
        assert len(calls) == 1
        entry: ReworkEvent = calls[0][0][0]
        assert entry.rework_reason == "local_retry"
        assert entry.ticket_id == "WOR-10"

    def test_rework_on_check_failure_escalation_records_escalated(
        self, tmp_path: Path
    ) -> None:
        """Check failure with escalation records both local_retry and escalated."""
        manifest = make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
            failure_policy=FailurePolicy(
                on_check_failure="abort", escalate_to_cloud=True
            ),
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
            patch("app.core.watcher_finalize.run_checks", return_value=False),
            patch("app.core.watcher_finalize.cleanup_worktree"),
        ):
            _call_finalize(worker, metrics=metrics_mock)

        calls = metrics_mock.record_rework_event.call_args_list
        assert len(calls) == 2
        local_retry = calls[0][0][0]
        assert local_retry.rework_reason == "local_retry"
        escalated = calls[1][0][0]
        assert escalated.rework_reason == "escalated"

    def test_rework_on_nonzero_exit_escalated(self, tmp_path: Path) -> None:
        """Non-zero returncode with escalation records reason=escalated."""
        manifest = make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
            failure_policy=FailurePolicy(escalate_to_cloud=True),
        )
        metrics_mock = MagicMock()
        worker = ActiveWorker(
            ticket_id="WOR-10",
            linear_id="fake-linear-id",
            manifest=manifest,
            worktree_path=tmp_path,
            process=MagicMock(spec=subprocess.Popen),
        )

        with patch("app.core.watcher_finalize.cleanup_worktree"):
            _call_finalize(worker, returncode=1, metrics=metrics_mock)

        calls = metrics_mock.record_rework_event.call_args_list
        assert len(calls) == 1
        entry: ReworkEvent = calls[0][0][0]
        assert entry.rework_reason == "escalated"
        assert entry.ticket_id == "WOR-10"

    def test_no_rework_on_nonzero_exit_no_escalation(self, tmp_path: Path) -> None:
        """Non-zero returncode without escalation does not record rework."""
        manifest = make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
        )
        metrics_mock = MagicMock()
        worker = ActiveWorker(
            ticket_id="WOR-10",
            linear_id="fake-linear-id",
            manifest=manifest,
            worktree_path=tmp_path,
            process=MagicMock(spec=subprocess.Popen),
        )

        with patch("app.core.watcher_finalize.cleanup_worktree"):
            _call_finalize(worker, returncode=1, metrics=metrics_mock)

        metrics_mock.record_rework_event.assert_not_called()

    def test_rework_on_escalate_action(self, tmp_path: Path) -> None:
        """_handle_policy_outcome action=escalate records reason=escalated."""
        manifest = make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
        )
        metrics_mock = MagicMock()
        linear_mock = MagicMock()

        worker = ActiveWorker(
            ticket_id="WOR-10",
            linear_id="fake-linear-id",
            manifest=manifest,
            worktree_path=tmp_path,
            process=MagicMock(spec=subprocess.Popen),
        )

        result_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / "result.json"
        result_path.write_text(
            json.dumps({"status": "success", "scope_drift": True}),
            encoding="utf-8",
        )

        with (
            patch("app.core.watcher_finalize.run_checks", return_value=True),
            patch("app.core.watcher_finalize.preserve_worker_artifacts"),
            patch(
                "app.core.watcher_finalize.create_pr",
                return_value="https://github.com/example/pr/1",
            ),
            patch("app.core.watcher_finalize.cleanup_worktree"),
        ):
            finalize_worker(
                worker,
                returncode=0,
                wall_time=1.0,
                linear=linear_mock,
                metrics=metrics_mock,
                escalation_policy=EscalationPolicy.from_toml(),
                repo_root=tmp_path,
                mode="default",
                project_id=_DEFAULT_PROJECT,
            )

        calls = metrics_mock.record_rework_event.call_args_list
        assert len(calls) == 1
        entry: ReworkEvent = calls[0][0][0]
        assert entry.rework_reason == "escalated"
        assert entry.ticket_id == "WOR-10"

    def test_rework_on_sonar_escalation(self, tmp_path: Path) -> None:
        """Sonar blocker triggers escalation and records reason=escalated."""
        manifest = make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
        )
        metrics_mock = MagicMock()
        linear_mock = MagicMock()

        worker = ActiveWorker(
            ticket_id="WOR-10",
            linear_id="fake-linear-id",
            manifest=manifest,
            worktree_path=tmp_path,
            process=MagicMock(spec=subprocess.Popen),
        )

        result_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / "result.json"
        result_path.write_text(
            json.dumps({"status": "success"}),
            encoding="utf-8",
        )

        with (
            patch("app.core.watcher_finalize.run_checks", return_value=True),
            patch("app.core.watcher_finalize.preserve_worker_artifacts"),
            patch("app.core.watcher_finalize.create_pr") as mock_create_pr,
            patch("app.core.watcher_finalize.cleanup_worktree"),
            patch(
                "app.core.watcher_finalize.fetch_sonar_findings",
                return_value=["BLOCKER"],
            ),
        ):
            finalize_worker(
                worker,
                returncode=0,
                wall_time=1.0,
                linear=linear_mock,
                metrics=metrics_mock,
                escalation_policy=EscalationPolicy.from_toml(),
                repo_root=tmp_path,
                mode="default",
                project_id=_DEFAULT_PROJECT,
            )

        calls = metrics_mock.record_rework_event.call_args_list
        assert len(calls) == 1
        entry: ReworkEvent = calls[0][0][0]
        assert entry.rework_reason == "escalated"
        assert entry.ticket_id == "WOR-10"
        mock_create_pr.assert_not_called()

    def test_no_rework_on_success(self, tmp_path: Path) -> None:
        """Successful finalization does not record any rework events."""
        manifest = make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
        )
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
            patch("app.core.watcher_finalize.run_checks", return_value=True),
            patch(
                "app.core.watcher_finalize.create_pr",
                return_value="https://github.com/example/pr/1",
            ),
            patch("app.core.watcher_finalize.cleanup_worktree"),
        ):
            _call_finalize(
                worker,
                metrics=metrics_mock,
                linear=linear_mock,
            )

        metrics_mock.record_rework_event.assert_not_called()
