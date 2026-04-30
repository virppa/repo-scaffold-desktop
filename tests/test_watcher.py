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
from app.core.watcher import Watcher, _ProcessedTicket
from app.core.watcher_types import ActiveWorker

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
# Watcher verbose flag
# ---------------------------------------------------------------------------


def test_watcher_verbose_defaults_to_false() -> None:
    w = Watcher(linear_client=MagicMock())
    assert w._verbose is False


def test_watcher_stores_verbose_true() -> None:
    w = Watcher(linear_client=MagicMock(), verbose=True)
    assert w._verbose is True


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
        patch("app.core.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.launch_worker", return_value=fake_process),
        patch.object(w._services, "ensure_ollama_running"),
        patch.object(w._services, "ensure_litellm_running"),
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
        patch("app.core.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.write_worker_pytest_config"),
        patch.object(watcher._services, "ensure_ollama_running"),
        patch.object(watcher._services, "ensure_litellm_running"),
        patch.object(watcher._services, "probe_vllm_health"),
        patch("app.core.watcher.launch_worker", return_value=fake_local_process),
    ):
        watcher._start_ticket("WOR-10", "fake-local-id")

    assert len(watcher._local_active) == 1
    assert watcher._local_active[0].ticket_id == "WOR-10"
    assert len(watcher._cloud_active) == 1
    assert watcher._cloud_active[0].ticket_id == "WOR-99"


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — ollama/litellm wiring
# ---------------------------------------------------------------------------


def test_dispatch_calls_ensure_litellm_but_not_ollama_for_local_effective_mode(
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
        patch("app.core.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.safe_set_state"),
        patch("app.core.watcher.backup_plan_files", return_value=[]),
        patch("app.core.watcher.launch_worker", return_value=fake_process),
        patch.object(w._services, "ensure_ollama_running") as mock_ollama,
        patch.object(w._services, "ensure_litellm_running") as mock_litellm,
        patch.object(w._services, "probe_vllm_health") as mock_probe,
    ):
        w._dispatch_next_ticket()

    mock_ollama.assert_not_called()
    mock_litellm.assert_called_once()
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
        patch("app.core.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.safe_set_state"),
        patch("app.core.watcher.backup_plan_files", return_value=[]),
        patch("app.core.watcher.launch_worker", return_value=fake_process),
        patch.object(w._services, "ensure_ollama_running") as mock_ollama,
        patch.object(w._services, "ensure_litellm_running") as mock_litellm,
        patch.object(w._services, "probe_vllm_health") as mock_probe,
    ):
        w._dispatch_next_ticket()

    mock_ollama.assert_not_called()
    mock_litellm.assert_not_called()
    mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_signal — SIGTERM triggers LiteLLM proxy cleanup and sets _running=False
# ---------------------------------------------------------------------------


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
# _handle_signal — SIGTERM triggers LiteLLM proxy cleanup and sets _running=False
# ---------------------------------------------------------------------------


def test_handle_signal_sigterm_terminates_litellm_proc_and_stops_running() -> None:
    w = Watcher(linear_client=MagicMock())
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 99999
    w._services._litellm_proc = mock_proc

    w._handle_signal(signal.SIGTERM, None)

    mock_proc.terminate.assert_called_once()
    assert w._services._litellm_proc is None
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
        patch("app.core.watcher.create_worktree") as mock_create,
        patch("app.core.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.safe_set_state") as mock_set_state,
        patch("app.core.watcher.backup_plan_files", return_value=[]),
        patch("app.core.watcher.launch_worker", return_value=fake_process),
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
        patch("app.core.watcher.create_worktree", return_value=tmp_path) as mock_create,
        patch("app.core.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.safe_set_state"),
        patch("app.core.watcher.backup_plan_files", return_value=[]),
        patch("app.core.watcher.launch_worker", return_value=fake_process),
        patch.object(w._services, "probe_vllm_health", return_value=True),
        patch.object(w._services, "ensure_ollama_running"),
        patch.object(w._services, "ensure_litellm_running"),
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
        patch("app.core.watcher.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.copy_manifest_to_worktree"),
        patch("app.core.watcher.write_worker_pytest_config"),
        patch("app.core.watcher.safe_set_state"),
        patch("app.core.watcher.backup_plan_files", return_value=[]),
        patch("app.core.watcher.launch_worker", return_value=fake_process),
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
