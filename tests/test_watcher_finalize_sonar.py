"""Tests for finalize_worker — sonar findings and severity classification."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
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
# _sonar_requires_escalation — boundary cases (AC)
# ---------------------------------------------------------------------------


def test_sonar_requires_escalation_empty_list(tmp_path: Path) -> None:
    """returns False for empty findings list."""
    linear_mock = MagicMock()
    from app.core.watcher.watcher_finalize import _sonar_requires_escalation

    assert (
        _sonar_requires_escalation(
            [], "WOR-10", "fake-id", linear_mock, EscalationPolicy.from_toml()
        )
        is False
    )


def test_sonar_requires_escalation_severity_triggers_true() -> None:
    """returns True when escalation_policy maps severity to 'escalate'."""
    linear_mock = MagicMock()
    from app.core.watcher.watcher_finalize import _sonar_requires_escalation

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
    from app.core.watcher.watcher_finalize import _sonar_requires_escalation

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
