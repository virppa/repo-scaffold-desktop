"""Tests for app.core.watcher_finalize — free finalization functions."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import LinearError
from app.core.manifest import FailurePolicy
from app.core.metrics import TicketMetrics
from app.core.watcher.watcher_finalize import _try_post_comment, finalize_worker
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
    compute_tags_fn=None,
) -> None:
    with patch(
        "app.core.watcher.watcher_finalize.compute_tags",
        compute_tags_fn or MagicMock(return_value=[]),
    ):
        finalize_worker(
            worker,
            returncode=returncode,
            wall_time=wall_time,
            linear=linear or MagicMock(),
            metrics=metrics or MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=repo_root or Path("."),
            mode=mode,
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(False, [])),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
    check_results = [(False, []), (False, []), (True, [])]
    with (
        patch(
            "app.core.watcher.watcher_finalize.run_checks", side_effect=check_results
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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

    with patch("app.core.watcher.watcher_finalize.cleanup_worktree"):
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_tokens is None
    assert m.context_compactions is None


# ---------------------------------------------------------------------------
# WOR-262: taxonomy fields propagate from manifest to ticket_metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_copies_taxonomy_fields_to_metrics(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        change_type="additive",
        reasoning_demand=4,
        scope_clarity=5,
        constraint_density=2,
        ac_specificity=4,
        tech_stack="python,sqlite,pydantic",
        raw_extensions='[".py",".md"]',
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.change_type == "additive"
    assert m.reasoning_demand == 4
    assert m.scope_clarity == 5
    assert m.constraint_density == 2
    assert m.ac_specificity == 4
    assert m.tech_stack == "python,sqlite,pydantic"
    assert m.raw_extensions == '[".py",".md"]'


def test_finalize_worker_taxonomy_none_when_manifest_lacks_them(tmp_path: Path) -> None:
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.change_type is None
    assert m.reasoning_demand is None
    assert m.scope_clarity is None
    assert m.constraint_density is None
    assert m.ac_specificity is None
    assert m.tech_stack is None
    assert m.raw_extensions is None


# ---------------------------------------------------------------------------
# WOR-348: effort propagates from manifest to ticket_metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_copies_effort_to_metrics(tmp_path: Path) -> None:
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        effort="xhigh",
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.effort == "xhigh"


def test_finalize_worker_effort_none_when_manifest_lacks_it(tmp_path: Path) -> None:
    """Manifest with no effort field → metrics.effort is None (default)."""
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.effort is None


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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize.fetch_sonar_findings",
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize.fetch_sonar_findings", return_value=None
        ),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize.fetch_sonar_findings",
            return_value=["BLOCKER"],
        ),
        patch("app.core.watcher.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize.fetch_sonar_findings",
            return_value=["MAJOR"],
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize.fetch_sonar_findings", return_value=None
        ),
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
# Human policy action — _handle_policy_outcome 'human' branch (lines 201-209)
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
# _try_post_comment exception guard (lines 257-258)
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


# ---------------------------------------------------------------------------
# _sonar_requires_escalation — boundary cases (AC)
# ---------------------------------------------------------------------------


def test_sonar_requires_escalation_empty_list(tmp_path: Path) -> None:
    """returns False for empty findings list."""
    from app.core.watcher.watcher_finalize import _sonar_requires_escalation

    assert (
        _sonar_requires_escalation([], "WOR-10", EscalationPolicy.from_toml()) is False
    )


def test_sonar_requires_escalation_severity_triggers_true() -> None:
    """returns True when escalation_policy maps severity to 'escalate'."""
    from app.core.watcher.watcher_finalize import _sonar_requires_escalation

    # Default policy: BLOCKER → escalate
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
    from app.core.watcher.watcher_finalize import _sonar_requires_escalation

    # Default policy: MAJOR, MINOR, INFO → fix_locally (not escalate)
    assert (
        _sonar_requires_escalation(
            ["MAJOR", "MINOR", "INFO"],
            "WOR-10",
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

    with caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"):
        # Should NOT raise — catches LinearError internally
        from app.core.watcher.watcher_finalize import safe_set_state

        safe_set_state(linear_mock, "fake-linear-id", "Blocked", "WOR-10")

    # set_state was called but the exception was caught and not re-raised
    linear_mock.set_state.assert_called_once_with("fake-linear-id", "Blocked")
    assert any("set_state failed" in msg for msg in caplog.messages)


def test_safe_set_state_success_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Successful set_state produces no warning log."""
    linear_mock = MagicMock()
    with caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"):
        from app.core.watcher.watcher_finalize import safe_set_state

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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_input_tokens is None
    assert m.local_output_tokens is None
    assert m.local_tokens is None
    assert m.local_output_tokens_per_second is None


# ---------------------------------------------------------------------------
# WOR-277 — waste_score wired to metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_waste_score_passed_to_metrics(tmp_path: Path) -> None:
    """Waste score from the worker log is passed to TicketMetrics."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "worker_wor-10.log"
    # Write a log with redundant reads to produce a non-zero waste score.
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Add redundant Read tool_use entries.
    for _ in range(3):
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "a.py"},
                    }
                )
                + "\n"
            )

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.waste_score is not None
    assert m.waste_score > 0
    assert m.waste_breakdown_json is not None


def test_finalize_worker_waste_score_none_when_no_log(
    tmp_path: Path,
) -> None:
    """When no worker log exists, waste_score is None."""
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
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.waste_score is None
    assert m.waste_breakdown_json is None


def test_finalize_worker_waste_warning_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING is logged when waste score exceeds threshold."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    metrics_mock = MagicMock()

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "worker_wor-10.log"
    # Write a log with many redundant reads to produce a high waste score.
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 1000, "output_tokens": 200},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # 20 redundant reads → 20 * 2 = 40, well above threshold of 60 with other signals.
    for _ in range(25):
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"path": "a.py"},
                    }
                )
                + "\n"
            )

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    # Should log a WARNING with the ticket ID and waste score.
    assert any("WOR-10" in msg and "waste" in msg.lower() for msg in caplog.messages)


# ---------------------------------------------------------------------------
# WOR-332 — tags auto-populated from result.json flags
# ---------------------------------------------------------------------------


def test_finalize_worker_auto_populates_tags_from_flags(tmp_path: Path) -> None:
    """When result.json has scope_drift, compute_tags fires and set_tags is called."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "scope_drift": True}),
        encoding="utf-8",
    )

    # Real TicketMetrics so compute_tags can read fields without MagicMock errors.
    row = TicketMetrics(
        ticket_id="WOR-10",
        project_id=_DEFAULT_PROJECT,
        implementation_mode="local",
        outcome="success",
        retry_count=0,
        lines_changed=5,
    )
    metrics_mock.get_by_ticket.return_value = row

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.preserve_worker_artifacts",
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker,
            linear=linear_mock,
            metrics=metrics_mock,
            repo_root=tmp_path,
            compute_tags_fn=MagicMock(return_value=["scope_drift"]),
        )

    # set_tags should be called because compute_tags returns non-empty list
    metrics_mock.set_tags.assert_called_once()
    call_args = metrics_mock.set_tags.call_args[0]
    assert call_args[0] == "WOR-10"
    assert call_args[1] == _DEFAULT_PROJECT
    assert "scope_drift" in call_args[2]


def test_finalize_worker_no_set_tags_when_compute_tags_empty(
    tmp_path: Path,
) -> None:
    """When compute_tags returns [], set_tags is NOT called."""
    manifest = make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({"status": "success"}), encoding="utf-8")

    # Real TicketMetrics so compute_tags doesn't crash on MagicMock comparisons.
    row = TicketMetrics(
        ticket_id="WOR-10",
        project_id=_DEFAULT_PROJECT,
        implementation_mode="local",
        outcome="success",
        retry_count=0,
        lines_changed=5,
    )
    metrics_mock.get_by_ticket.return_value = row

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch("app.core.watcher.watcher_finalize.run_checks", return_value=(True, [])),
        patch(
            "app.core.watcher.watcher_finalize.preserve_worker_artifacts",
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    metrics_mock.set_tags.assert_not_called()
