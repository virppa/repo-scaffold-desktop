"""Smoke tests for app.core.watcher.dispatch.start_ticket.

The function was extracted from Watcher._start_ticket during the WOR-253
reorg and went uncovered because tests mocked it at the call site rather
than exercising its body. These tests cover the four main paths
(local-happy, cloud-happy, vllm-not-ready defer, cloud-pool-full defer)
plus the dedup-on-repeat-defer edge case.
"""

from __future__ import annotations

import logging
import subprocess
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
        )

    assert len(local_active) == 1
    assert len(cloud_active) == 0
    mock_create.assert_called_once()
    services.ensure_vllm_anthropic_mode.assert_called_once()


def test_start_ticket_cloud_happy_path_appends_to_cloud_active(tmp_path: Path) -> None:
    """Cloud-mode dispatch: launches without vLLM probe, appends to cloud_active."""
    manifest = make_manifest(implementation_mode="cloud")
    linear = MagicMock()

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
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

    linear.get_open_blockers.return_value = []
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
            _dedup_state={},
        )

    assert len(local_active) == 1
    linear.post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# WOR-297 — dedup state survives across consecutive calls
# ---------------------------------------------------------------------------

# The fix: _dedup_state was previously Optional (default None), so callers
# passing None or not passing it at all got a fresh empty dict on every
# call, defeating cross-call dedup memoization. Now it's required dict[str,
# str], so the caller passes the same mutable dict across calls and the
# suppress_dedup state persists.


def test_start_ticket_vllm_not_ready_dedup_logs_warning_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two consecutive vLLM-not-ready deferrals log the warning only once."""
    manifest = make_manifest(implementation_mode="local")
    linear = MagicMock()

    linear.get_open_blockers.return_value = []
    services = _services(vllm_healthy=False)
    local_active: list = []
    cloud_active: list = []
    dedup_state: dict[str, str] = {}

    with caplog.at_level(logging.WARNING, logger="app.core.watcher.dispatch"):
        # First call — not yet in dedup state → logs warning.
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
            _dedup_state=dedup_state,
        )
        # Second call — same condition already tracked → suppressed.
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
            _dedup_state=dedup_state,
        )

    vllm_warnings = [r for r in caplog.records if "vLLM not ready" in r.message]
    assert len(vllm_warnings) == 1  # second call is suppressed


# ---------------------------------------------------------------------------
# WOR-419 — epic-branch overlap defense gate at dispatch
# ---------------------------------------------------------------------------


def test_start_ticket_defers_when_another_epic_branch_already_in_flight(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dispatch to a new epic/* branch is refused when another epic/* branch
    is already in-flight on a local worker."""
    manifest = make_manifest(
        implementation_mode="local",
        base_branch="epic/wor-419-new-epic",
    )
    # Create an active worker on a different epic branch
    active_epic_manifest = make_manifest(
        implementation_mode="local",
        base_branch="epic/wor-335-active",
    )
    from app.core.watcher.watcher_types import ActiveWorker

    local_active: list[ActiveWorker] = [
        ActiveWorker(
            ticket_id="WOR-335-A",
            linear_id="fake-linear-id-335",
            manifest=active_epic_manifest,
            worktree_path=tmp_path / "worktree_335",
            process=MagicMock(spec=subprocess.Popen),
        )
    ]
    linear = MagicMock()

    linear.get_open_blockers.return_value = []
    services = _services()
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
            linear_id="fake-linear-id-419",
            ticket_id="WOR-419",
            _escalation_policy=MagicMock(),
            _dedup_state={},
        )

    mock_create.assert_not_called()
    assert len(local_active) == 1  # no worker appended, original preserved
    # Linear comment should have been posted
    linear.post_comment.assert_called_once()
    body = linear.post_comment.call_args[0][1]
    assert "epic branch" in body.lower()
    assert "epic/wor-335-active" in body
    assert "epic/wor-419-new-epic" in body
    assert any("epic branch" in r.message for r in caplog.records)


def test_start_ticket_proceeds_for_same_epic_branch(
    tmp_path: Path,
) -> None:
    """Dispatch within the SAME epic branch is unaffected — normal dispatch path."""
    # WOR-431: dispatch.start_ticket now does path-overlap checking. Use
    # distinct allowed_paths so the existing-worker fixture doesn't cause
    # an unrelated overlap deferral.
    epic_manifest = make_manifest(
        implementation_mode="local",
        base_branch="epic/wor-335-active",
        allowed_paths=["app/core/new_file.py"],
    )
    from app.core.watcher.watcher_types import ActiveWorker

    existing_worker_manifest = make_manifest(
        implementation_mode="local",
        base_branch="epic/wor-335-active",
        allowed_paths=["app/core/other_file.py"],
    )
    local_active: list[ActiveWorker] = [
        ActiveWorker(
            ticket_id="WOR-335-A",
            linear_id="fake-linear-id-335",
            manifest=existing_worker_manifest,
            worktree_path=tmp_path / "worktree_335",
            process=MagicMock(spec=subprocess.Popen),
        )
    ]
    linear = MagicMock()

    linear.get_open_blockers.return_value = []
    services = _services()
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
            manifest=epic_manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id-419",
            ticket_id="WOR-419",
            _escalation_policy=MagicMock(),
            _dedup_state={},
        )

    assert len(local_active) == 2  # both the existing and new worker
    linear.post_comment.assert_not_called()


def test_start_ticket_unaffected_when_no_epic_workers_active(
    tmp_path: Path,
) -> None:
    """A non-epic base_branch dispatches normally when no epic workers are active."""
    manifest = make_manifest(
        implementation_mode="local",
        base_branch="main",
    )
    local_active: list = []  # no active workers at all
    cloud_active: list = []
    linear = MagicMock()

    linear.get_open_blockers.return_value = []
    services = _services()

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
            _dedup_state={},
        )

    assert len(local_active) == 1
    linear.post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# WOR-290 — cloud_only refusal under --worker-mode local
# ---------------------------------------------------------------------------


def test_start_ticket_refuses_cloud_only_when_local_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Manifest with routing=cloud_only and daemon at --worker-mode local
    is refused — no worktree created, Linear comment posted, ticket
    stays in ReadyForLocal."""
    manifest = make_manifest(
        routing="cloud_only",
        implementation_mode="local",
    )
    linear = MagicMock()
    linear.get_open_blockers.return_value = []
    services = _services(mode="local")  # --worker-mode local
    local_active: list = []
    cloud_active: list = []

    with (
        patch("app.core.watcher.dispatch.create_worktree") as mock_create,
        patch("app.core.watcher.dispatch.safe_set_state") as mock_set,
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
            ticket_id="WOR-290",
            _escalation_policy=MagicMock(),
            _dedup_state={},
        )

    mock_create.assert_not_called()
    mock_set.assert_not_called()
    assert local_active == []
    assert cloud_active == []
    assert any("cloud_only" in r.message for r in caplog.records)
    linear.post_comment.assert_called_once()
    body = linear.post_comment.call_args[0][1]
    assert "local-only mode" in body
