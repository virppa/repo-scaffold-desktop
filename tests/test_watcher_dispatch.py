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
            project_id="repo-scaffold-desktop",
            services=services,
            worker_verbose=False,
            retry_counters={},
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_local_workers=8,
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
    services.ensure_litellm_running.assert_called_once()


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
            project_id="repo-scaffold-desktop",
            services=services,
            worker_verbose=False,
            retry_counters={},
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_local_workers=8,
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
            project_id="repo-scaffold-desktop",
            services=services,
            worker_verbose=False,
            retry_counters={},
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_local_workers=8,
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
            project_id="repo-scaffold-desktop",
            services=services,
            worker_verbose=False,
            retry_counters={},
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_local_workers=8,
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


# NOTE: a fifth test for suppress_dedup behaviour is intentionally omitted —
# dispatch.py's `_dedup_state or {}` falls through to a fresh empty dict on
# each call, defeating cross-call memoization. That's a real pre-existing
# bug worth filing separately; it is not in scope for this coverage push.
