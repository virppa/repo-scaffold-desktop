"""Tests for human_escalate wiring in watcher_finalize_helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
from app.core.watcher.watcher_finalize_helpers import _execute_finalization
from app.core.watcher.watcher_types import ActiveWorker
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# Helpers
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
        process=MagicMock(),
    )
    return linear_mock, worker


# ---------------------------------------------------------------------------
# human_trigger — pause for review (direct _execute_finalization tests)
# ---------------------------------------------------------------------------


def test_human_trigger_architecture_change_pauses(tmp_path: Path) -> None:
    """architecture_change → comment posted, outcome=aborted, no PR."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "human_trigger": "architecture_change"}),
        encoding="utf-8",
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(),
    )

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks", return_value=(True, [])
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ):
            with patch(
                "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"
            ):
                with patch(
                    "app.core.watcher.watcher_finalize_helpers.squash_wip_commits"
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

    assert outcome == "aborted"
    assert escalated is False
    assert preserved is True
    assert findings is None
    linear_mock.post_comment.assert_called_once()
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "architecture_change" in comment_body
    assert "WOR-10" in comment_body
    attempt_fn.assert_not_called()


def test_human_trigger_schema_migration_pauses(tmp_path: Path) -> None:
    """schema_migration → comment posted, outcome=aborted, no PR."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "human_trigger": "schema_migration"}),
        encoding="utf-8",
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(),
    )

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks", return_value=(True, [])
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ):
            with patch(
                "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"
            ):
                with patch(
                    "app.core.watcher.watcher_finalize_helpers.squash_wip_commits"
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

    assert outcome == "aborted"
    assert escalated is False
    assert preserved is True
    assert findings is None
    linear_mock.post_comment.assert_called_once()
    attempt_fn.assert_not_called()


def test_human_trigger_cross_module_refactor_pauses(tmp_path: Path) -> None:
    """cross_module_refactor → comment posted, outcome=aborted, no PR."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "human_trigger": "cross_module_refactor"}),
        encoding="utf-8",
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(),
    )

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks", return_value=(True, [])
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ):
            with patch(
                "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"
            ):
                with patch(
                    "app.core.watcher.watcher_finalize_helpers.squash_wip_commits"
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

    assert outcome == "aborted"
    assert escalated is False
    assert preserved is True
    assert findings is None
    linear_mock.post_comment.assert_called_once()
    attempt_fn.assert_not_called()


def test_human_trigger_auth_payments_touched_pauses(tmp_path: Path) -> None:
    """auth_payments_touched → comment posted, outcome=aborted, no PR."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "human_trigger": "auth_payments_touched"}),
        encoding="utf-8",
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(),
    )

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks", return_value=(True, [])
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ):
            with patch(
                "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"
            ):
                with patch(
                    "app.core.watcher.watcher_finalize_helpers.squash_wip_commits"
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

    assert outcome == "aborted"
    assert escalated is False
    assert preserved is True
    assert findings is None
    linear_mock.post_comment.assert_called_once()
    attempt_fn.assert_not_called()


# ---------------------------------------------------------------------------
# No trigger / empty trigger — normal path unchanged
# ---------------------------------------------------------------------------


def test_no_human_trigger_normal_success(tmp_path: Path) -> None:
    """No human_trigger in result → normal success path (PR created)."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text('{"status": "success"}', encoding="utf-8")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(),
    )

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks", return_value=(True, [])
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ):
            with patch(
                "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"
            ):
                with patch(
                    "app.core.watcher.watcher_finalize_helpers.squash_wip_commits"
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
    assert escalated is False
    assert preserved is True
    assert findings is None
    assert attempt_fn.called


def test_empty_human_trigger_proceeds_normally(tmp_path: Path) -> None:
    """human_trigger: '' (empty string) is ignored → normal path."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "human_trigger": ""}),
        encoding="utf-8",
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(),
    )

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks", return_value=(True, [])
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ):
            with patch(
                "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"
            ):
                with patch(
                    "app.core.watcher.watcher_finalize_helpers.squash_wip_commits"
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
    assert escalated is False
    assert preserved is True
    assert attempt_fn.called


def test_unknown_trigger_raises_value_error(tmp_path: Path) -> None:
    """Unknown trigger value → ValueError from classify_human_trigger."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "human_trigger": "unknown_trigger"}),
        encoding="utf-8",
    )
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(),
    )

    with patch(
        "app.core.watcher.watcher_finalize_helpers.run_checks", return_value=(True, [])
    ):
        with patch(
            "app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"
        ):
            with pytest.raises(ValueError, match="Unknown human trigger"):
                _execute_finalization(
                    worker,
                    0,
                    linear_mock,
                    EscalationPolicy.from_toml(),
                    tmp_path,
                    MagicMock(),
                )


# ---------------------------------------------------------------------------
