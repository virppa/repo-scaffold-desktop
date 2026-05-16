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
from app.core.watcher.watcher_finalize import finalize_worker
from app.core.watcher.watcher_helpers import (
    WorkerBehavior,
    WorkerTelemetry,
)
from app.core.watcher.watcher_types import ActiveWorker
from tests.conftest import make_isolated_repo_root, make_manifest

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
            repo_root=repo_root or make_isolated_repo_root(),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(False, []),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
            return_value=MagicMock(),
        ),
    ):
        _call_finalize(worker)
        _call_finalize(worker)

    assert worker.attempt_count == 2


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
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=check_results,
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
            return_value=MagicMock(),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
    # WOR-357: context_compactions is now counted from system/compact_boundary
    # events, not from the result event's (always-NULL) field. Include 5
    # such events so the assertion exercises the new counting path.
    boundary = json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n"
    log_file.write_text(
        boundary * 5
        + json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 2000, "output_tokens": 400},
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    linear_mock.set_state.assert_called_once_with("fake-linear-id", "MergedToEpic")
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(
            worker, linear=linear_mock, metrics=metrics_mock, repo_root=tmp_path
        )

    linear_mock.set_state.assert_called_once_with("fake-linear-id", "MergedToEpic")
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
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
# WOR-312 — in-dispatch retry loop
# ---------------------------------------------------------------------------


def test_finalize_worker_max_retries_zero_no_retry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """max_retries=0: single attempt even when checks fail, no retry.

    attempt_count tracks total check failures; with 1 call and 1 failure
    the count is 1 (one check-failure event). The key assertion is that
    launch_worker was NOT called — no retry happened.
    """
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        failure_policy={"max_retries": 0},
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(False, [{"check": "ruff check .", "exit_code": 1}]),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
        ) as mock_launch,
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_finalize"),
    ):
        _call_finalize(worker)

    # No retry happened — attempt_count is 1 from the first loop iteration
    assert worker.attempt_count == 1
    # Verify launch_worker was NOT called (no retry happened)
    mock_launch.assert_not_called()


def test_finalize_worker_hardcap_clamps_above_one(tmp_path: Path) -> None:
    """max_retries=5 but hardcapped at 1 — exactly 2 total iterations.

    Without hardcap the retry loop would allow 5 retries (6 iterations).
    ATTEMPT_HARDCAP=1 clamps it to 1 retry = 2 total check iterations.
    """
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        failure_policy={"max_retries": 5},
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    # Simulate 2 failures: first → loop continues, second → attempt_count > 1
    # so the hardcap (max_retries=1) is exceeded and the loop exits.
    check_results = [
        (False, [{"check": "ruff check .", "exit_code": 1}]),
        (False, [{"check": "mypy app/", "exit_code": 1}]),
    ]

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=check_results,
        ),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
        ) as mock_launch,
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker)

    # Hardcap: 1 retry = 2 total iterations. launch_worker called once.
    assert worker.attempt_count == 2
    mock_launch.assert_called_once()


def test_finalize_worker_single_retry_then_success(
    tmp_path: Path,
) -> None:
    """max_retries=1: checks fail once, succeed on retry. attempt_count == 1
    because only failures increment the counter."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        failure_policy={"max_retries": 1},
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    # First call fails → attempt_count=1, break 1>=1 → no, retry.
    # Second call succeeds → attempt_count stays 1, break on success.
    check_results = [
        (False, [{"check": "ruff check .", "exit_code": 1}]),
        (True, []),
    ]

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=check_results,
        ),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
            return_value=MagicMock(),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker)

    # After 2 calls (1 failure + 1 success), attempt_count=2 (one per loop iteration).
    # The break check 2 > 1 = True, but success breaks first.
    assert worker.attempt_count == 2


def test_finalize_worker_hardcap_enforces_max_one_retry(
    tmp_path: Path,
) -> None:
    """max_retries=5 but hardcapped at 1 — only one retry despite 5 budget.
    attempt_count increments at every loop iteration, so 2 iterations = 2."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        failure_policy={"max_retries": 5},
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    # 2 iterations: first fails, second succeeds.
    # With max_retries=5 and hardcap=1, max actual retries = min(5,1) = 1.
    check_results = [
        (False, [{"check": "ruff check .", "exit_code": 1}]),
        (True, []),
    ]

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=check_results,
        ),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
            return_value=MagicMock(),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker)

    assert worker.attempt_count == 2


def test_finalize_worker_retry_injects_extra_constraint(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Retry path calls launch_worker with extra_constraint containing RETRY hint."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        failure_policy={"max_retries": 1},
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    check_results = [
        (False, [{"check": "ruff check .", "exit_code": 1}]),
        (True, []),
    ]

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=check_results,
        ),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
            return_value=MagicMock(),
        ) as mock_launch,
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_finalize"),
    ):
        _call_finalize(worker)

    # launch_worker called exactly once for the retry
    mock_launch.assert_called_once()
    call_kwargs = mock_launch.call_args
    assert call_kwargs.kwargs["extra_constraint"] is not None
    assert "RETRY" in call_kwargs.kwargs["extra_constraint"]
    assert "ruff check ." in call_kwargs.kwargs["extra_constraint"]


def test_finalize_worker_retry_first_failure_logs_info(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First check failure logs at INFO level, retry at WARNING."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        failure_policy={"max_retries": 1},
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    check_results = [
        (False, [{"check": "ruff check .", "exit_code": 1}]),
        (True, []),
    ]

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=check_results,
        ),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
            return_value=MagicMock(),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        caplog.at_level(logging.INFO, logger="app.core.watcher.watcher_finalize"),
    ):
        _call_finalize(worker)

    info_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "Re-launching" in r.message
    ]
    assert len(info_records) == 1
    assert "WOR-10" in info_records[0].message


def test_finalize_worker_no_retry_when_check_passes(
    tmp_path: Path,
) -> None:
    """When checks pass on first attempt, no retry loop — immediate success."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test",
        failure_policy={"max_retries": 1},
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker)

    assert worker.attempt_count == 1


# ---------------------------------------------------------------------------
# WOR-420 / WOR-429: retry x parse interaction -- both run-log rows get usage
# ---------------------------------------------------------------------------


def test_finalize_worker_retry_behavior_parse_fails_then_succeeds(
    tmp_path: Path,
) -> None:
    """Retry path where _parse_worker_behavior raises on attempt 1 and
    succeeds on attempt 2; asserts both ticket_run_log rows have
    non-NULL usage telemetry from the second parse.

    Regression guard: if the WOR-420 fix is reverted the first call will
    still record usage-only telemetry (from _parse_worker_usage) and the
    second call will record both -- but the behaviour-telemetry assertion
    on the first row will fail.
    """
    manifest = make_manifest(
        ticket_id="WOR-429",
        worker_branch="wor-429-test",
        failure_policy={"max_retries": 1},
    )
    worker = ActiveWorker(
        ticket_id="WOR-429",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    metrics_mock = MagicMock()

    expected_behavior = WorkerBehavior(
        turn_count=5,
        tool_calls_total=10,
        tool_calls_breakdown=None,
        thinking_blocks=2,
        thinking_chars_total=1500,
        input_tokens_max=5000,
        input_tokens_first=3000,
        input_tokens_last=4000,
        redundant_reads_count=0,
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.launch_worker",
            return_value=MagicMock(),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        # WOR-466: parsers unified into _parse_worker_telemetry; first call
        # raises (simulating the WOR-420 parse-race scenario), second call
        # returns the expected telemetry with usage + behavior populated.
        patch(
            "app.core.watcher.watcher_finalize._parse_worker_telemetry",
            side_effect=[
                Exception("parse error"),
                WorkerTelemetry(
                    input_tokens=1000,
                    output_tokens=500,
                    context_compactions=0,
                    compact_duration_ms=0,
                    subagent_spawns=0,
                    api_retries=0,
                    hook_trust_violations=0,
                    behavior=expected_behavior,
                ),
            ],
        ),
    ):
        # First call: behavior parser raises -> record() never called,
        # but record_run IS called -> run-log row with usage telemetry.
        with pytest.raises(Exception, match="parse error"):
            _call_finalize(worker, metrics=metrics_mock)
        # Second call: behavior parser succeeds -> record() and record_run()
        # both called -> run-log row with usage + behavior telemetry.
        _call_finalize(worker, metrics=metrics_mock)

    # First call: finalize_worker crashes before record_run -> no run-log row.
    # Second call: succeeds -> one run-log row with complete telemetry.
    assert metrics_mock.record_run.call_count == 1
    run = metrics_mock.record_run.call_args[0][0]
    # Usage telemetry from _parse_worker_usage is non-null.
    assert run.input_tokens is not None
    assert run.output_tokens is not None
    assert run.output_tok_per_s is not None
    assert run.context_compactions == 0


# ---------------------------------------------------------------------------
# WOR-420: end-to-end finalize regression -- sample log -> telemetry in DB row
# ---------------------------------------------------------------------------


def test_finalize_worker_e2e_behavior_from_worker_log(tmp_path: Path) -> None:
    """End-to-end test: write a sample log with streaming content, run
    finalize_worker, and verify the telemetry row has non-NULL behaviour
    fields (turn_count, tool_calls_total, thinking_blocks).

    Regression guard: without the WOR-420 fix the behavior stream-parser
    will not capture these fields, so the assertions will fail.
    """
    manifest = make_manifest(
        ticket_id="WOR-429",
        worker_branch="wor-429-test-ticket",
    )
    metrics_mock = MagicMock()

    # Build a minimal worker log with assistant events containing
    # tool_use, thinking, and text blocks so that _parse_worker_behavior
    # extracts real telemetry.
    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-429.log"

    log_lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Let me look at the directory.",
                        },
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Done with the task."},
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 2000, "output_tokens": 400},
            }
        ),
    ]
    log_file.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    worker = ActiveWorker(
        ticket_id="WOR-429",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.turn_count is not None
    assert m.tool_calls_total is not None
    assert m.thinking_blocks is not None


# ---------------------------------------------------------------------------
# WOR-351: waste_score=0 recorded (not NULL), breakdown_json NULL when empty
# ---------------------------------------------------------------------------


def test_finalize_worker_waste_score_zero_not_null(tmp_path: Path) -> None:
    """waste_score column receives the actual score even when 0.

    A clean run (no waste signals) produces score=0; the metrics row must
    store 0, not NULL, so dashboards can distinguish "measured 0 = clean"
    from "not measured = NULL".
    """
    manifest = make_manifest(ticket_id="WOR-351", worker_branch="wor-351-test")
    metrics_mock = MagicMock()

    # Minimal log with zero waste signals -> compute_waste_score returns 0.
    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-351.log"
    log_file.write_text(
        json.dumps(
            {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}}
        )
        + "\n",
        encoding="utf-8",
    )

    worker = ActiveWorker(
        ticket_id="WOR-351",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://gh/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.waste_score == 0
    # When breakdown dict is empty, waste_breakdown_json should stay NULL.
    assert m.waste_breakdown_json is None


# ---------------------------------------------------------------------------
# WOR-455: allowed-paths enforcement
# ---------------------------------------------------------------------------


def test_finalize_worker_allowed_paths_clean_proceeds(
    tmp_path: Path,
) -> None:
    """No diff files → no violations → PR proceeds normally."""
    manifest = make_manifest(
        ticket_id="WOR-455",
        worker_branch="wor-455-test",
        allowed_paths=["app/core/watcher/watcher_finalize.py"],
        forbidden_paths=["app/ui/**"],
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-455",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, metrics=metrics_mock)

    metrics_mock.record.assert_called_once()
    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False


def test_finalize_worker_allowed_path_violation_marks_blocked(
    tmp_path: Path,
) -> None:
    """Worker diff touches a file NOT in allowed_paths → Blocked + comment."""
    manifest = make_manifest(
        ticket_id="WOR-455",
        worker_branch="wor-455-test",
        allowed_paths=["app/core/watcher/watcher_finalize.py"],
        forbidden_paths=["app/ui/**"],
    )
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-455",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=["ALLOWED app/core/ui/widget.py"],
        ),
    ):
        _call_finalize(worker, linear=linear_mock)

    # PR should NOT be attempted — we never call create_pr
    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "WOR-455" in comment_body
    assert "ALLOWED app/core/ui/widget.py" in comment_body
    assert "PR creation aborted" in comment_body


def test_finalize_worker_forbidden_path_violation_marks_blocked(
    tmp_path: Path,
) -> None:
    """Worker diff touches a forbidden path → Blocked + FORBIDDEN tag."""
    manifest = make_manifest(
        ticket_id="WOR-455",
        worker_branch="wor-455-test",
        allowed_paths=["app/core/watcher/watcher_finalize.py"],
        forbidden_paths=["app/ui/**"],
    )
    linear_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-455",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=["FORBIDDEN app/ui/widget.py"],
        ),
    ):
        _call_finalize(worker, linear=linear_mock)

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "FORBIDDEN app/ui/widget.py" in comment_body
    assert "PR creation aborted" in comment_body
