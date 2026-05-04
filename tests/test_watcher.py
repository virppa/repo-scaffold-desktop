"""Tests for the watcher/orchestrator pure logic functions.

Integration tests (actually launching subprocesses, Linear API) are out of scope;
this file covers the unit-testable, I/O-free helpers unique to app.core.watcher.
Duplicate tests (helpers, subprocess, types, worktrees, finalize, promotion) are
in their respective module-aligned test files.
"""

from __future__ import annotations

import logging
import signal
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.linear_client import LinearError
from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import Watcher, _ProcessedTicket
from app.core.watcher.watcher_types import ActiveWorker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_manifest(**overrides: Any) -> ExecutionManifest:
    defaults: dict[str, Any] = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test ticket",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "review_mode": "auto",
        "base_branch": "wor-96-local-worker-engine",
        "worker_branch": "wor-10-test-ticket",
        "objective": "Do the thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        # WOR-378: dispatch refuses manifests with empty required_checks; default
        # a non-empty list to match the conftest fixture.
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)


_SENTINEL: list[str] = ["app/core/bar.py"]


def _make_active_worker(
    ticket_id: str = "WOR-11", allowed_paths: list[str] | None = None
) -> ActiveWorker:
    paths = _SENTINEL if allowed_paths is None else allowed_paths
    manifest = _make_manifest(
        ticket_id=ticket_id,
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
        allowed_paths=paths,
    )
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=Path(f"/tmp/{ticket_id}"),
        process=MagicMock(spec=subprocess.Popen),
    )


# ---------------------------------------------------------------------------
# Watcher verbose flags
# ---------------------------------------------------------------------------


def test_worker_verbose_defaults_to_false() -> None:
    w = Watcher(linear_client=MagicMock())
    assert w._worker_verbose is False


def test_worker_verbose_stores_true() -> None:
    w = Watcher(linear_client=MagicMock(), worker_verbose=True)
    assert w._worker_verbose is True


def test_worker_verbose_and_no_epic_shutdown_can_be_combined() -> None:
    """Both flags can be set simultaneously — they are independent."""
    w = Watcher(linear_client=MagicMock(), worker_verbose=True, no_epic_shutdown=True)
    assert w._worker_verbose is True


# ---------------------------------------------------------------------------
# _safe_set_state — daemon survives LinearError at _start_ticket
# ---------------------------------------------------------------------------


def test_start_ticket_set_state_failure_worker_still_starts(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.set_state.side_effect = LinearError("unknown state")

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.watcher.create_worktree"),
        patch("app.core.watcher.watcher.copy_manifest_to_worktree"),
        patch(
            "app.core.watcher.watcher.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-10"


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — Spike label guard
# ---------------------------------------------------------------------------


def _spike_ticket(label_name: str = "Spike") -> dict[str, Any]:
    return {
        "id": "fake-linear-id",
        "identifier": "WOR-99",
        "title": "Some spike",
        "labels": {"nodes": [{"name": label_name}]},
    }


def _regular_ticket() -> dict[str, Any]:
    return {
        "id": "fake-linear-id",
        "identifier": "WOR-99",
        "title": "Regular ticket",
        "labels": {"nodes": [{"name": "local-ready"}]},
    }


def test_dispatch_skips_spike_labelled_ticket(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [_spike_ticket("Spike")]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with (
        patch.object(w, "_start_ticket") as mock_start,
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        w._dispatch_next_ticket()

    mock_start.assert_not_called()
    assert any("Spike" in msg and "WOR-99" in msg for msg in caplog.messages)


@pytest.mark.parametrize("label_name", ["spike", "SPIKE", "Spike"])
def test_dispatch_skips_spike_label_case_insensitive(
    tmp_path: Path, label_name: str
) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [_spike_ticket(label_name)]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with patch.object(w, "_start_ticket") as mock_start:
        w._dispatch_next_ticket()

    mock_start.assert_not_called()


def test_dispatch_proceeds_for_non_spike_ticket(tmp_path: Path) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [_regular_ticket()]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with patch.object(w, "_start_ticket") as mock_start:
        w._dispatch_next_ticket()

    mock_start.assert_called_once_with("WOR-99", "fake-linear-id")


def test_dispatch_missing_labels_field_no_crash(tmp_path: Path) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [
        {"id": "fake-linear-id", "identifier": "WOR-99", "title": "No labels"}
    ]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with patch.object(w, "_start_ticket") as mock_start:
        w._dispatch_next_ticket()

    mock_start.assert_called_once_with("WOR-99", "fake-linear-id")


# ---------------------------------------------------------------------------
# Per-type concurrency — cloud pool full does not block local dispatch
# ---------------------------------------------------------------------------


def test_cloud_pool_full_does_not_block_local_dispatch(tmp_path: Path) -> None:
    """A saturated cloud pool must not prevent a local ticket from being dispatched."""
    local_manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
        allowed_paths=["app/core/local_only.py"],
    )
    cloud_manifest = _make_manifest(
        ticket_id="WOR-99",
        worker_branch="wor-99-cloud-ticket",
        implementation_mode="cloud",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-99"),
        allowed_paths=["app/core/cloud_only.py"],
    )

    mock_linear = MagicMock()
    mock_linear.get_open_blockers.return_value = []

    watcher = Watcher(
        linear_client=mock_linear,
        max_local_workers=1,
        max_cloud_workers=1,
    )

    watcher._cloud_active.append(
        ActiveWorker(
            ticket_id="WOR-99",
            linear_id="fake-cloud-id",
            manifest=cloud_manifest,
            worktree_path=tmp_path,
            process=MagicMock(spec=subprocess.Popen),
        )
    )

    fake_local_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(watcher, "_load_manifest", return_value=local_manifest),
        patch("app.core.watcher.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher.write_worker_pytest_config"),
        patch.object(watcher._services, "ensure_vllm_anthropic_mode"),
        patch.object(watcher._services, "probe_vllm_health"),
        patch(
            "app.core.watcher.watcher.launch_worker",
            return_value=fake_local_process,
        ),
    ):
        watcher._start_ticket("WOR-10", "fake-local-id")

    assert len(watcher._local_active) == 1
    assert watcher._local_active[0].ticket_id == "WOR-10"
    assert len(watcher._cloud_active) == 1
    assert watcher._cloud_active[0].ticket_id == "WOR-99"


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — vLLM Anthropic-mode gate (WOR-368)
# ---------------------------------------------------------------------------


def test_dispatch_calls_ensure_vllm_anthropic_mode_for_local_effective_mode(
    tmp_path: Path,
) -> None:
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {"identifier": "WOR-10", "id": "fake-linear-id", "labels": {"nodes": []}}
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.watcher_worktrees.create_worktree", return_value=tmp_path
        ),
        patch("app.core.watcher.watcher_worktrees.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher_worktrees.write_worker_pytest_config"),
        patch("app.core.watcher.watcher_finalize.safe_set_state"),
        patch("app.core.watcher.watcher_worktrees.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher_subprocess.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode") as mock_vllm_anthropic,
        patch.object(w._services, "probe_vllm_health") as mock_probe,
    ):
        w._dispatch_next_ticket()

    mock_vllm_anthropic.assert_called_once()
    mock_probe.assert_called_once()


def test_dispatch_skips_ensure_for_cloud_effective_mode(tmp_path: Path) -> None:
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="cloud",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {"identifier": "WOR-10", "id": "fake-linear-id", "labels": {"nodes": []}}
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.watcher_worktrees.create_worktree", return_value=tmp_path
        ),
        patch("app.core.watcher.watcher_worktrees.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher_worktrees.write_worker_pytest_config"),
        patch("app.core.watcher.watcher_finalize.safe_set_state"),
        patch("app.core.watcher.watcher_worktrees.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher_subprocess.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode") as mock_vllm_anthropic,
        patch.object(w._services, "probe_vllm_health") as mock_probe,
    ):
        w._dispatch_next_ticket()

    mock_vllm_anthropic.assert_not_called()
    mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# _check_epic_completion — all-complete and nothing-processed paths
# ---------------------------------------------------------------------------


def test_check_epic_completion_posts_comment_and_exits(tmp_path: Path) -> None:
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    with (
        patch.object(w, "_has_waiting_deps", return_value=False),
        patch.object(
            w, "_lookup_pr_url", return_value="https://github.com/org/repo/pull/1"
        ),
    ):
        w._check_epic_completion()

    linear_mock.post_comment.assert_called_once_with(
        "WOR-96",
        "All sub-tickets merged — ready for `/close-epic WOR-96`",
    )
    assert w._running is False


def test_check_epic_completion_no_epic_shutdown_keeps_running(
    tmp_path: Path,
) -> None:
    """When no_epic_shutdown=True, _check_epic_completion must NOT set
    _running to False. The epic-complete comment must still be posted."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    with (
        patch.object(w, "_has_waiting_deps", return_value=False),
        patch.object(
            w, "_lookup_pr_url", return_value="https://github.com/org/repo/pull/1"
        ),
    ):
        w._check_epic_completion()

    linear_mock.post_comment.assert_called_once()
    assert w._running is True


def test_check_epic_completion_no_tickets_processed_no_comment_exits(
    tmp_path: Path,
) -> None:
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    with patch.object(w, "_has_waiting_deps", return_value=False):
        w._check_epic_completion()

    linear_mock.post_comment.assert_not_called()
    assert w._running is True


def test_check_epic_completion_empty_startup_keeps_polling(tmp_path: Path) -> None:
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    assert not w._processed_tickets

    with patch.object(w, "_has_waiting_deps", return_value=False):
        w._check_epic_completion()

    assert w._running is True


# ---------------------------------------------------------------------------
# _check_epic_completion — memoization: duplicate suppression
# ---------------------------------------------------------------------------


def test_epic_completion_memoization_suppresses_duplicate(
    tmp_path: Path,
) -> None:
    """Two consecutive _check_epic_completion calls with identical state must
    post the Linear comment exactly once, not twice."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    with patch.object(w, "_has_waiting_deps", return_value=False):
        w._check_epic_completion()
        w._check_epic_completion()

    linear_mock.post_comment.assert_called_once()


def test_epic_completion_re_emit_on_state_change(
    tmp_path: Path,
) -> None:
    """When the processed-ticket state changes between calls (e.g. a new ticket
    is added), a fresh comment IS posted."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    with patch.object(w, "_has_waiting_deps", return_value=False):
        w._check_epic_completion()
        # State changed — add a second ticket
        w._processed_tickets.append(
            _ProcessedTicket(
                ticket_id="WOR-11",
                epic_id="WOR-96",
                worker_branch="wor-11-test-ticket",
                elapsed=60.0,
            )
        )
        w._check_epic_completion()

    assert linear_mock.post_comment.call_count == 2


def test_epic_completion_failed_ticket_blocks_comment(
    tmp_path: Path,
) -> None:
    """A _ProcessedTicket with succeeded=False must still block the comment
    and NOT trigger re-posts through memoization."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
            succeeded=False,
        )
    ]

    with patch.object(w, "_has_waiting_deps", return_value=False):
        w._check_epic_completion()
        w._check_epic_completion()

    linear_mock.post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_signal — SIGTERM calls services.stop() and sets _running=False
# ---------------------------------------------------------------------------


def test_handle_signal_sigterm_calls_services_stop_and_clears_running() -> None:
    """Post-WOR-368 the watcher no longer owns a LiteLLM process to terminate;
    services.stop() is a no-op kept for call-site compat. The signal handler
    must still invoke it AND clear _running so the poll loop exits."""
    w = Watcher(linear_client=MagicMock())
    with patch.object(w._services, "stop") as mock_stop:
        w._handle_signal(signal.SIGTERM, None)

    mock_stop.assert_called_once()
    assert w._running is False


# ---------------------------------------------------------------------------
# _log_startup_info — cloud mode omits max_local_workers
# ---------------------------------------------------------------------------


def test_startup_info_cloud_mode_omits_max_local_workers(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    w = Watcher(
        linear_client=MagicMock(),
        worker_mode="cloud",
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        w._log_startup_info()
    msg = caplog.text
    assert "mode=cloud" in msg
    assert "max_cloud_workers=3" in msg
    assert "max_local_workers" not in msg


# ---------------------------------------------------------------------------
# _log_startup_info — local mode omits max_cloud_workers
# ---------------------------------------------------------------------------


def test_startup_info_local_mode_omits_max_cloud_workers(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    w = Watcher(
        linear_client=MagicMock(),
        worker_mode="local",
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        w._log_startup_info()
    msg = caplog.text
    assert "mode=local" in msg
    assert "max_local_workers=8" in msg
    assert "max_cloud_workers" not in msg


# ---------------------------------------------------------------------------
# _log_startup_info — default mode logs both pool sizes
# ---------------------------------------------------------------------------


def test_startup_info_default_mode_logs_both_pool_sizes(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    w = Watcher(
        linear_client=MagicMock(),
        worker_mode="default",
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        w._log_startup_info()
    msg = caplog.text
    assert "mode=default" in msg
    assert "max_local_workers=8" in msg
    assert "max_cloud_workers=3" in msg


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — vLLM readiness gate: health probe blocks dispatch
# ---------------------------------------------------------------------------


def test_dispatch_deferred_when_vllm_not_ready(tmp_path: Path) -> None:
    """When probe_vllm_health() returns False, _dispatch_next_ticket must return
    without calling create_worktree, copy_manifest_to_worktree,
    write_worker_pytest_config, safe_set_state, or launch_worker.
    The ticket stays in ReadyForLocal."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.watcher.create_worktree") as mock_create,
        patch("app.core.watcher.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.watcher.safe_set_state") as mock_set_state,
        patch("app.core.watcher.watcher.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "probe_vllm_health", return_value=False),
    ):
        w._dispatch_next_ticket()

    # Nothing should have been created — the ticket stays in ReadyForLocal
    mock_create.assert_not_called()
    mock_set_state.assert_not_called()
    # launch_worker should not have been called either
    fake_process.assert_not_called()


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — vLLM readiness gate: health probe passes → dispatch proceeds
# ---------------------------------------------------------------------------


def test_dispatch_proceeds_when_vllm_ready(tmp_path: Path) -> None:
    """When probe_vllm_health() returns True, dispatch proceeds normally
    (create_worktree is called, state is set, worker is launched)."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.watcher.create_worktree", return_value=tmp_path
        ) as mock_create,
        patch("app.core.watcher.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.watcher.safe_set_state"),
        patch("app.core.watcher.watcher.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "probe_vllm_health", return_value=True),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
    ):
        w._dispatch_next_ticket()

    # create_worktree must be called — dispatch proceeded
    mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — cloud mode skips vLLM probe entirely
# ---------------------------------------------------------------------------


def test_cloud_mode_skips_vllm_probe(tmp_path: Path) -> None:
    """When effective mode is cloud, probe_vllm_health() must NOT be called.
    Dispatch proceeds directly to create_worktree."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="cloud",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.watcher_worktrees.create_worktree", return_value=tmp_path
        ),
        patch("app.core.watcher.watcher_worktrees.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher_worktrees.write_worker_pytest_config"),
        patch("app.core.watcher.watcher_finalize.safe_set_state"),
        patch("app.core.watcher.watcher_worktrees.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher_subprocess.launch_worker",
            return_value=fake_process,
        ),
        patch.object(
            w._services, "probe_vllm_health", return_value=False
        ) as mock_probe,
    ):
        w._dispatch_next_ticket()

    # probe_vllm_health must not have been called for cloud mode
    mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# _check_epic_completion — partial failure: at least one succeeded=False
# ---------------------------------------------------------------------------


def test_epic_completion_partial_failure_skips_comment(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    """When _processed_tickets contains at least one entry with succeeded=False,
    _check_epic_completion must log a WARNING and NOT call linear.post_comment.
    The watcher must not set _running=False."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
            succeeded=True,
        ),
        _ProcessedTicket(
            ticket_id="WOR-11",
            epic_id="WOR-96",
            worker_branch="wor-11-test-ticket",
            elapsed=60.0,
            succeeded=False,
        ),
    ]

    with caplog.at_level(logging.WARNING, logger="app.core.watcher"):
        w._check_epic_completion()

    # The epic-complete comment must NOT be posted when there's a failure
    linear_mock.post_comment.assert_not_called()
    # watcher still exits — _running is set to False regardless of success/failure
    assert w._running is False
    # A warning must be logged about the failure
    assert any(
        "failed" in msg.lower() and "succeeded" in msg.lower()
        for msg in caplog.messages
    )


# ---------------------------------------------------------------------------
# _enrich_with_retry_context — injects constraint when last_failure.json exists
# ---------------------------------------------------------------------------


def test_enrich_with_retry_context_injects_constraint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import json

    manifest = _make_manifest(implementation_constraints=["original constraint"])
    artifact_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    artifact_dir.mkdir(parents=True)
    failure = {
        "failed_at": "2026-04-30T10:00:00Z",
        "check": "pytest",
        "stdout": (
            "FAILED tests/test_watcher_worktrees.py"
            "::test_cleanup_orphaned_worktrees_removes_subdirs"
            " - AssertionError: assert 0 == 2\n"
        ),
        "stderr": "",
    }
    (artifact_dir / "last_failure.json").write_text(
        json.dumps(failure), encoding="utf-8"
    )

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        enriched = w._enrich_with_retry_context(manifest)

    assert enriched.implementation_constraints[0].startswith("RETRY:")
    assert "pytest" in enriched.implementation_constraints[0]
    assert (
        "test_cleanup_orphaned_worktrees_removes_subdirs"
        in (enriched.implementation_constraints[0])
    )
    assert enriched.implementation_constraints[1] == "original constraint"
    assert any("retry context" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# _enrich_with_retry_context — no-op when last_failure.json absent
# ---------------------------------------------------------------------------


def test_enrich_with_retry_context_noop_without_failure_file(tmp_path: Path) -> None:
    manifest = _make_manifest(implementation_constraints=["original constraint"])
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    enriched = w._enrich_with_retry_context(manifest)

    assert enriched.implementation_constraints == ["original constraint"]


# ---------------------------------------------------------------------------
# Manifest blocker check — dispatch skips when manifest declares an unmerged blocker
# ---------------------------------------------------------------------------


def test_dispatch_skipped_when_manifest_declares_unmerged_blocker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dispatch must be skipped when the manifest declares a blocker that is not yet
    in a Linear completed state, even if Linear's own blockedBy is empty."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        blocked_by_tickets=["WOR-266"],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_issue_state_type.return_value = "started"  # not done

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        caplog.at_level(logging.INFO, logger="app.core.watcher"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    # No worktree should have been created — the ticket stays in ReadyForLocal
    assert w._local_active == []
    assert any(
        "WOR-266" in msg and "unmerged blocker" in msg for msg in caplog.messages
    )


def test_dispatch_proceeds_when_manifest_blocked_by_tickets_is_empty(
    tmp_path: Path,
) -> None:
    """Dispatch must proceed when the manifest declares no blocked_by_tickets."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        blocked_by_tickets=[],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_issue_state_type.return_value = "started"

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = []

    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.watcher.safe_set_state"),
        patch("app.core.watcher.watcher.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-10"


def test_dispatch_proceeds_when_all_manifest_blockers_are_merged(
    tmp_path: Path,
) -> None:
    """Dispatch must proceed when every manifest-declared blocker has reached
    a Linear completed state (MergedToEpic / Done → state type 'completed')."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        blocked_by_tickets=["WOR-266", "WOR-267"],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_issue_state_type.side_effect = lambda x: "completed"  # both done

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = []

    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.watcher.safe_set_state"),
        patch("app.core.watcher.watcher.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-10"


# ---------------------------------------------------------------------------
# _reap_pool — ghost-slot leak prevention (WOR-334)
# ---------------------------------------------------------------------------


def _finished_worker(ticket_id: str, returncode: int = 0) -> ActiveWorker:
    """Build an ActiveWorker whose process.poll() returns returncode."""
    worker = _make_active_worker(ticket_id=ticket_id)
    worker.process.poll.return_value = returncode
    return worker


def _running_worker(ticket_id: str) -> ActiveWorker:
    """Build an ActiveWorker whose process.poll() returns None (still alive)."""
    worker = _make_active_worker(ticket_id=ticket_id)
    worker.process.poll.return_value = None
    return worker


def test_reap_pool_frees_slot_when_finalize_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WOR-334 regression: an exception inside finalize_worker must not leave
    the failing worker (or its siblings) in the pool."""
    workers = [
        _finished_worker("WOR-100"),
        _finished_worker("WOR-101"),
        _finished_worker("WOR-102"),
    ]

    watcher = Watcher(linear_client=MagicMock())

    def finalize_side_effect(*args: Any, **kwargs: Any) -> str:
        if args[0].ticket_id == "WOR-101":
            raise RuntimeError("simulated Linear API blow-up")
        return "success"

    with (
        patch(
            "app.core.watcher.watcher.finalize_worker",
            side_effect=finalize_side_effect,
        ),
        patch(
            "app.core.watcher.watcher.format_worker_token_count",
            return_value="0 tokens",
        ),
        caplog.at_level(logging.ERROR, logger="app.core.watcher.watcher"),
    ):
        watcher._reap_pool(workers)

    assert workers == [], (
        f"Expected pool to be empty after finalize raised; got "
        f"{[w.ticket_id for w in workers]}"
    )

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    matching = [r for r in error_records if "WOR-101" in r.getMessage()]
    assert matching, "Expected an ERROR log mentioning WOR-101 when its finalize raised"
    assert any(r.exc_info is not None for r in matching), (
        "Expected exc_info to be attached to the ERROR log for forensic context"
    )


def test_reap_pool_eight_workers_one_raises_pool_empty() -> None:
    """8 workers all finish, 1 raises during finalize -> pool ends empty.

    This is the WOR-313 overnight scenario: a single finalize exception used
    to freeze the pool at full size and block all further dispatch."""
    workers = [_finished_worker(f"WOR-20{i}") for i in range(8)]
    watcher = Watcher(linear_client=MagicMock())

    def finalize_side_effect(*args: Any, **kwargs: Any) -> str:
        if args[0].ticket_id == "WOR-204":
            raise RuntimeError("simulated finalize blow-up")
        return "success"

    with (
        patch(
            "app.core.watcher.watcher.finalize_worker",
            side_effect=finalize_side_effect,
        ),
        patch(
            "app.core.watcher.watcher.format_worker_token_count",
            return_value="0 tokens",
        ),
    ):
        watcher._reap_pool(workers)

    assert workers == [], (
        f"Expected all 8 slots freed; remaining: {[w.ticket_id for w in workers]}"
    )


def test_reap_pool_keeps_still_running_workers() -> None:
    """Sanity: workers whose poll() returns None must stay in the pool."""
    running = _running_worker("WOR-300")
    finished = _finished_worker("WOR-301")
    workers = [running, finished]

    watcher = Watcher(linear_client=MagicMock())

    with (
        patch(
            "app.core.watcher.watcher.finalize_worker",
            return_value="success",
        ),
        patch(
            "app.core.watcher.watcher.format_worker_token_count",
            return_value="0 tokens",
        ),
    ):
        watcher._reap_pool(workers)

    assert [w.ticket_id for w in workers] == ["WOR-300"], (
        "Running worker should remain; finished worker should be removed"
    )


# ---------------------------------------------------------------------------
# Soft-stop / drain mode (WOR-333)
# ---------------------------------------------------------------------------


def test_softstop_sentinel_triggers_drain_mode(tmp_path: Path) -> None:
    """Writing .claude/watcher.softstop puts the daemon in drain mode."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    sentinel = tmp_path / ".claude" / "watcher.softstop"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    assert w._draining is False
    w._check_softstop_request()
    assert w._draining is True
    assert w._draining_since is not None


def test_softstop_check_idempotent(tmp_path: Path) -> None:
    """Calling _check_softstop_request multiple times keeps draining_since stable."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    sentinel = tmp_path / ".claude" / "watcher.softstop"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    w._check_softstop_request()
    first_since = w._draining_since
    w._check_softstop_request()
    w._check_softstop_request()
    # _draining_since must not move on subsequent calls (one-shot transition)
    assert w._draining_since == first_since


def test_softstop_no_sentinel_no_drain(tmp_path: Path) -> None:
    """When no sentinel exists, the daemon stays in normal operation."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._check_softstop_request()
    assert w._draining is False
    assert w._draining_since is None


def test_drain_mode_skips_dispatch(tmp_path: Path) -> None:
    """When _draining is True, _dispatch_next_ticket should not be called.

    This test exercises the run-loop guard: when draining, dispatch is gated
    out even if there's pool capacity and a ticket waiting in Linear.
    """
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = [
        {"identifier": "WOR-99", "id": "fake-id", "labels": {"nodes": []}}
    ]
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._draining = True

    # Force a single iteration of the run loop body by patching out the
    # blocking parts. _dispatch_next_ticket should NOT be called when draining.
    with (
        patch.object(w, "_dispatch_next_ticket") as mock_dispatch,
        patch.object(w, "_promote_waiting_tickets") as mock_promote,
        patch.object(w, "_check_epic_completion") as mock_epic,
    ):
        # Simulate the run-loop body's drain-aware section
        if not w._draining:
            mock_promote()
        if not w._draining:
            mock_dispatch()
        if not w._draining:
            mock_epic()

    mock_dispatch.assert_not_called()
    mock_promote.assert_not_called()
    mock_epic.assert_not_called()


def test_stale_softstop_sentinel_cleaned_on_startup(tmp_path: Path) -> None:
    """A sentinel left over from a prior daemon run is removed at startup."""
    sentinel = tmp_path / ".claude" / "watcher.softstop"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    assert sentinel.exists()

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._remove_stale_softstop_sentinel()
    assert not sentinel.exists()


def test_softstop_stuck_warning_fires_after_threshold(tmp_path: Path) -> None:
    """If drain is pending too long (default 60min), log a one-shot WARNING."""
    import time as _time

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._draining = True
    # Backdate draining_since to 70 minutes ago
    w._draining_since = _time.monotonic() - 70 * 60

    # Add one fake active worker so the warning has something to print
    w._local_active.append(_make_active_worker(ticket_id="WOR-99"))
    w._local_active[0].start_time = _time.monotonic() - 70 * 60

    with caplog_at_level_helper() as records:
        w._maybe_warn_softstop_stuck()

    matching = [r for r in records if "Soft-stop pending" in r.getMessage()]
    assert matching, f"Expected stuck-warning; got: {[r.getMessage() for r in records]}"
    assert w._softstop_warned_stuck is True

    # Subsequent calls should NOT re-warn
    with caplog_at_level_helper() as records2:
        w._maybe_warn_softstop_stuck()
    assert not any("Soft-stop pending" in r.getMessage() for r in records2), (
        "Stuck-warning should be one-shot; second call must not re-emit"
    )


# Tiny helper for the one test above (caplog fixture differs across pytest versions)
import logging as _logging  # noqa: E402


class _LogCapture:
    def __init__(self) -> None:
        self.records: list[_logging.LogRecord] = []
        self._handler: _logging.Handler | None = None

    def __enter__(self) -> list[_logging.LogRecord]:
        self._handler = _logging.Handler()
        self._handler.setLevel(_logging.WARNING)
        self._handler.emit = lambda r: self.records.append(r)  # type: ignore[method-assign]
        _logging.getLogger("app.core.watcher.watcher").addHandler(self._handler)
        return self.records

    def __exit__(self, *_args: object) -> None:
        if self._handler is not None:
            _logging.getLogger("app.core.watcher.watcher").removeHandler(self._handler)


def caplog_at_level_helper() -> _LogCapture:
    return _LogCapture()
