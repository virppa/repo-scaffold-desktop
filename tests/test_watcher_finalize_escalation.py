"""Tests for finalize_worker — escalation policy and state transitions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.escalation_policy import EscalationPolicy
from app.core.manifest import FailurePolicy
from app.core.watcher.watcher_types import ActiveWorker
from tests._finalize_helpers import _call_finalize
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# Shared helper
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


# ---------------------------------------------------------------------------
# EscalationPolicy flag routing
# ---------------------------------------------------------------------------


def test_finalize_worker_scope_drift_escalates(tmp_path: Path) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {"scope_drift": True})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False


# ---------------------------------------------------------------------------
# Human policy action — _handle_policy_outcome human branch
# ---------------------------------------------------------------------------


def test_finalize_worker_human_policy_posts_comment_and_aborts(
    tmp_path: Path,
) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {})
    metrics_mock = MagicMock()
    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch("app.core.watcher.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
    with patch("app.core.watcher.watcher_finalize.cleanup_worktree"):
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
    from app.core.watcher.watcher_finalize import _execute_finalization

    outcome, escalated, preserved, findings, _, _ = _execute_finalization(
        worker, 1, linear_mock, EscalationPolicy.from_toml(), tmp_path
    )

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
    from app.core.watcher.watcher_finalize import _execute_finalization

    with patch(
        "app.core.watcher.watcher_finalize.run_checks",
        return_value=(False, []),
    ):
        outcome, escalated, preserved, findings, _, _ = _execute_finalization(
            worker, 0, linear_mock, EscalationPolicy.from_toml(), tmp_path
        )

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
    from app.core.watcher.watcher_finalize import _handle_policy_outcome

    outcome, escalated, findings, pr_url = _handle_policy_outcome(
        "escalate",
        {"scope_drift": True},
        worker,
        linear_mock,
        EscalationPolicy.from_toml(),
    )

    assert outcome == "escalated"
    assert escalated is True
    assert findings is None
    assert pr_url is None


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
    from app.core.watcher.watcher_finalize import _handle_policy_outcome

    outcome, escalated, findings, pr_url = _handle_policy_outcome(
        "human",
        {"scope_drift": True},
        worker,
        linear_mock,
        EscalationPolicy.from_toml(),
    )

    assert outcome == "aborted"
    assert escalated is False
    assert findings is None
    assert pr_url is None
