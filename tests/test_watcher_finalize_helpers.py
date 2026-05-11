"""Tests for app.core.watcher_finalize_helpers — internal finalization helpers."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
from app.core.manifest import FailurePolicy
from app.core.watcher.watcher_finalize_helpers import (
    _execute_finalization,
    _handle_policy_outcome,
    _sonar_requires_escalation,
    _try_post_comment,
)
from app.core.watcher.watcher_types import ActiveWorker
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEFAULT_PROJECT = "repo-scaffold-desktop"


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
        process=MagicMock(spec=__import__("subprocess").Popen),
    )
    return linear_mock, worker


# ---------------------------------------------------------------------------
# _sonar_requires_escalation — boundary cases
# ---------------------------------------------------------------------------


def test_sonar_requires_escalation_empty_list(tmp_path: Path) -> None:
    """returns False for empty findings list."""
    assert (
        _sonar_requires_escalation([], "WOR-10", EscalationPolicy.from_toml()) is False
    )


def test_sonar_requires_escalation_severity_triggers_true() -> None:
    """returns True when escalation_policy maps severity to 'escalate'."""
    # Default policy: BLOCKER -> escalate
    assert (
        _sonar_requires_escalation(["BLOCKER"], "WOR-10", EscalationPolicy.from_toml())
        is True
    )
    assert (
        _sonar_requires_escalation(["CRITICAL"], "WOR-10", EscalationPolicy.from_toml())
        is True
    )


def test_sonar_requires_escalation_no_triggers_false() -> None:
    """returns False when no severity maps to 'escalate'."""
    # Default policy: MAJOR, MINOR, INFO -> fix_locally (not escalate)
    assert (
        _sonar_requires_escalation(
            ["MAJOR", "MINOR", "INFO"],
            "WOR-10",
            EscalationPolicy.from_toml(),
        )
        is False
    )


# ---------------------------------------------------------------------------
# _execute_finalization — explicit return-value assertions
# ---------------------------------------------------------------------------


def test_execute_finalization_nonzero_returncode_returns_failure(
    tmp_path: Path,
) -> None:
    """non-zero returncode -> 'failure' returned (not just logged)."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=__import__("subprocess").Popen),
    )

    outcome, escalated, preserved, findings, _, _ = _execute_finalization(
        worker, 1, linear_mock, EscalationPolicy.from_toml(), tmp_path, MagicMock()
    )

    assert outcome == "failure"
    assert escalated is False
    assert preserved is False
    assert findings is None


def test_execute_finalization_check_failure_abort_returns_failure(
    tmp_path: Path,
) -> None:
    """checks fail with on_check_failure='abort' -> 'failure' returned."""
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
        process=MagicMock(spec=__import__("subprocess").Popen),
    )
    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks",
        return_value=(False, []),
    ):
        outcome, escalated, preserved, findings, _, _ = _execute_finalization(
            worker, 0, linear_mock, EscalationPolicy.from_toml(), tmp_path, MagicMock()
        )

    assert outcome == "failure"
    assert escalated is False
    assert preserved is False
    assert findings is None


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
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=__import__("subprocess").Popen),
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(False, []),
        ),
        patch("app.core.watcher.watcher_worktrees.cleanup_worktree"),
    ):
        _execute_finalization(
            worker, 0, linear_mock, EscalationPolicy.from_toml(), tmp_path, MagicMock()
        )

    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    linear_mock.post_comment.assert_called_once()


def test_execute_finalization_check_failure_blocked_when_no_escalate(
    tmp_path: Path,
) -> None:
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
        process=MagicMock(spec=__import__("subprocess").Popen),
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(False, []),
        ),
        patch("app.core.watcher.watcher_worktrees.cleanup_worktree"),
    ):
        _execute_finalization(
            worker, 0, linear_mock, EscalationPolicy.from_toml(), tmp_path, MagicMock()
        )

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")


def test_execute_finalization_nonzero_exit_escalates_to_cloud(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        failure_policy=FailurePolicy(escalate_to_cloud=True),
    )
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=__import__("subprocess").Popen),
    )
    with patch("app.core.watcher.watcher_worktrees.cleanup_worktree"):
        _execute_finalization(
            worker, 1, linear_mock, EscalationPolicy.from_toml(), tmp_path, MagicMock()
        )

    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    linear_mock.post_comment.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_policy_outcome — explicit return-value assertions
# ---------------------------------------------------------------------------


def test_handle_policy_outcome_escalate_returns_escalated(
    tmp_path: Path,
) -> None:
    """action='escalate' -> returns 'escalated'."""
    linear_mock = MagicMock()
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=__import__("subprocess").Popen),
    )

    outcome, escalated, findings, pr_url = _handle_policy_outcome(
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
    assert pr_url is None


def test_handle_policy_outcome_human_returns_aborted(tmp_path: Path) -> None:
    """action='human' -> returns 'aborted'."""
    linear_mock = MagicMock()
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=__import__("subprocess").Popen),
    )

    outcome, escalated, findings, pr_url = _handle_policy_outcome(
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
    assert pr_url is None


# ---------------------------------------------------------------------------
# Human policy action — _handle_policy_outcome 'human' branch
# ---------------------------------------------------------------------------


def test_finalize_worker_human_policy_posts_comment_and_aborts(
    tmp_path: Path,
) -> None:
    linear_mock, worker = _make_worker_with_result(tmp_path, {})
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
        patch("app.core.watcher.watcher_subprocess.create_pr") as mock_create_pr,
        patch("app.core.watcher.watcher_worktrees.cleanup_worktree"),
        patch.object(EscalationPolicy, "classify_result", return_value="human"),
    ):
        from app.core.watcher.watcher_finalize import finalize_worker

        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_not_called()
    linear_mock.post_comment.assert_called_once()
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "Human review required" in comment_body
    assert "WOR-10" in comment_body


# ---------------------------------------------------------------------------
# _try_post_comment exception guard
# ---------------------------------------------------------------------------


def test_try_post_comment_swallows_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    linear_mock = MagicMock()
    linear_mock.post_comment.side_effect = Exception("connection reset by peer")

    with caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"):
        _try_post_comment(linear_mock, "lin-id", "WOR-10", "some comment body")

    assert any("Could not post comment" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# WOR-132: deferred SonarCloud fetch sets pending_sonar_fetch flag
# ---------------------------------------------------------------------------


def test_execute_finalization_sonar_none_sets_pending_flag(
    tmp_path: Path,
) -> None:
    """When fetch_sonar_findings returns None in the fix_locally path,
    the worker gets pending_sonar_fetch=True so the poll loop can retry."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=__import__("subprocess").Popen),
    )
    # result.json reports success so we enter the fix_locally path
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text('{"status": "success"}', encoding="utf-8")

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks",
        return_value=(True, []),
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ):
            with patch(
                "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts",
            ):
                with patch(
                    "app.core.watcher.watcher_finalize_helpers.squash_wip_commits",
                ):
                    attempt_fn = MagicMock(return_value=("success", "https://pr.url"))
                    outcome, escalated, preserved, findings, _, _ = (
                        _execute_finalization(
                            worker,
                            0,
                            linear_mock,
                            EscalationPolicy.from_toml(),
                            tmp_path,
                            attempt_fn,
                        )
                    )

    assert outcome == "success"
    assert findings is None
    assert worker.pending_sonar_fetch is True
    assert worker.sonar_fetch_attempts == 1
    assert worker.sonar_first_attempted_at is not None
