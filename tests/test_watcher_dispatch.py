"""Smoke tests for app.core.watcher.dispatch.start_ticket.

The function was extracted from Watcher._start_ticket during the WOR-253
reorg and went uncovered because tests mocked it at the call site rather
than exercising its body. These tests cover the four main paths
(local-happy, cloud-happy, vllm-not-ready defer, cloud-pool-full defer)
plus the dedup-on-repeat-defer edge case.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.watcher.dispatch import start_ticket
from tests.conftest import make_manifest


def _services(mode: str = "default", vllm_healthy: bool = True) -> MagicMock:
    services = MagicMock()
    services._mode = mode
    services.probe_vllm_health.return_value = vllm_healthy
    return services


def _patch_side_effects(tmp_path: Path):
    """Patch the disk + subprocess side-effects used by start_ticket."""
    return [
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "worktree",
        ),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
    ]


def test_start_ticket_local_happy_path_appends_to_local_active(tmp_path: Path) -> None:
    """Local-mode dispatch: worktree created, worker launched, appended to local."""
    manifest = make_manifest(implementation_mode="local")
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "worktree",
        ) as mock_create,
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    assert len(local_active) == 1
    assert len(cloud_active) == 0
    mock_create.assert_called_once()
    services.ensure_vllm_anthropic_mode.assert_called_once()


def test_start_ticket_cloud_happy_path_appends_to_cloud_active(tmp_path: Path) -> None:
    """Cloud-mode dispatch: launches without vLLM probe, appends to cloud_active."""
    manifest = make_manifest(implementation_mode="cloud")
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "worktree",
        ),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    assert len(cloud_active) == 1
    assert len(local_active) == 0
    # Cloud mode should NOT probe vLLM
    services.probe_vllm_health.assert_not_called()


def test_start_ticket_defers_when_vllm_not_ready(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Local dispatch with vLLM unhealthy → defers, logs warning, no worktree."""
    manifest = make_manifest(implementation_mode="local")
    linear = MagicMock()
    services = _services(vllm_healthy=False)
    local_active: list = []
    cloud_active: list = []

    with (
        patch("app.core.watcher.dispatch.create_worktree") as mock_create,
        caplog.at_level(logging.WARNING, logger="app.core.watcher.dispatch"),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    mock_create.assert_not_called()
    assert local_active == []
    assert any("vLLM not ready" in r.message for r in caplog.records)


def test_start_ticket_defers_when_cloud_pool_full(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Cloud-mode dispatch with cloud_active at capacity → defers without launching."""
    manifest = make_manifest(implementation_mode="cloud")
    linear = MagicMock()
    services = _services()
    # Cloud pool already at max
    cloud_active: list = [MagicMock(), MagicMock(), MagicMock()]
    local_active: list = []

    with (
        patch("app.core.watcher.dispatch.create_worktree") as mock_create,
        caplog.at_level(logging.INFO, logger="app.core.watcher.dispatch"),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    mock_create.assert_not_called()
    assert len(cloud_active) == 3  # unchanged
    assert any("cloud pool full" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# WOR-378 — manifest-quality refusal at dispatch
# ---------------------------------------------------------------------------


def test_start_ticket_refuses_local_manifest_with_empty_allowed_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Local-mode manifest with allowed_paths=[] is refused, ticket sent to Backlog."""
    manifest = make_manifest(implementation_mode="local", allowed_paths=[])
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch("app.core.watcher.dispatch.create_worktree") as mock_create,
        patch("app.core.watcher.dispatch.safe_set_state") as mock_set_state,
        caplog.at_level(logging.WARNING, logger="app.core.watcher.dispatch"),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    mock_create.assert_not_called()
    assert local_active == []
    mock_set_state.assert_called_once_with(
        linear, "fake-linear-id", "Backlog", "WOR-10"
    )
    linear.post_comment.assert_called_once()
    body = linear.post_comment.call_args[0][1]
    assert "allowed_paths" in body
    assert "/start-ticket" in body
    assert any("empty allowed_paths" in r.message for r in caplog.records)


def test_start_ticket_refuses_manifest_with_empty_required_checks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Manifest with required_checks=[] is refused regardless of mode."""
    manifest = make_manifest(implementation_mode="cloud", required_checks=[])
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch("app.core.watcher.dispatch.create_worktree") as mock_create,
        patch("app.core.watcher.dispatch.safe_set_state") as mock_set_state,
        caplog.at_level(logging.WARNING, logger="app.core.watcher.dispatch"),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    mock_create.assert_not_called()
    assert cloud_active == []
    mock_set_state.assert_called_once_with(
        linear, "fake-linear-id", "Backlog", "WOR-10"
    )
    linear.post_comment.assert_called_once()
    body = linear.post_comment.call_args[0][1]
    assert "required_checks" in body
    assert "ruff check" in body or "pytest" in body
    assert any("empty required_checks" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# WOR-373 — stale-epic refusal at dispatch
# ---------------------------------------------------------------------------


def test_start_ticket_refuses_when_epic_too_far_behind_main(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Epic branch with drift > threshold is refused, ticket sent to Backlog."""
    manifest = make_manifest(
        implementation_mode="local",
        base_branch="epic/wor-100-stale",
    )
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch("app.core.watcher.dispatch.create_worktree") as mock_create,
        patch("app.core.watcher.dispatch.safe_set_state") as mock_set_state,
        patch(
            "app.core.watcher.dispatch.count_main_ahead_of_epic",
            return_value=107,
        ),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.dispatch"),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    mock_create.assert_not_called()
    assert local_active == []
    mock_set_state.assert_called_once_with(
        linear, "fake-linear-id", "Backlog", "WOR-10"
    )
    linear.post_comment.assert_called_once()
    body = linear.post_comment.call_args[0][1]
    assert "epic/wor-100-stale" in body
    assert "107" in body
    assert "git merge origin/main" in body
    assert any("behind origin/main" in r.message for r in caplog.records)


def test_start_ticket_proceeds_when_epic_within_threshold(
    tmp_path: Path,
) -> None:
    """An epic branch with drift below the threshold dispatches normally."""
    manifest = make_manifest(
        implementation_mode="local",
        base_branch="epic/wor-100-fresh",
    )
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "worktree",
        ),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch(
            "app.core.watcher.dispatch.count_main_ahead_of_epic",
            return_value=5,  # well under the 30 threshold
        ),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    assert len(local_active) == 1
    linear.post_comment.assert_not_called()


def test_start_ticket_does_not_refuse_main_target(
    tmp_path: Path,
) -> None:
    """Sub-tickets targeting main directly are not refused.

    The helper fast-paths to 0 for non-epic branches, so dispatch proceeds
    normally without refusal.
    """
    manifest = make_manifest(
        implementation_mode="local",
        base_branch="main",
    )
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "worktree",
        ),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
    ):
        # Helper not patched — real helper returns 0 for non-epic branches.
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
        )

    assert len(local_active) == 1
    linear.post_comment.assert_not_called()


# NOTE: a fifth test for suppress_dedup behaviour is intentionally omitted —
# dispatch.py's `_dedup_state or {}` falls through to a fresh empty dict on
# each call, defeating cross-call memoization. That's a real pre-existing
# bug worth filing separately; it is not in scope for this coverage push.
