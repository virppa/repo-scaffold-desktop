"""Tests for app.core.watcher.watcher.Watcher class methods.

Bumps watcher.py coverage from 66% by exercising methods that existing
tests inlined logic against MagicMocks rather than calling the real
Watcher methods:

- _log_startup_banner (per-mode formatting)
- _check_softstop_sentinel
- _check_pause_sentinel + _resume
- _check_forcestop_sentinel (incl. commit_wip_state + terminate)
- _check_kill_sentinel (incl. multi-ticket + missing-ticket paths)
- _emit_post_iteration_signals
- _finalize_run

Pattern: construct Watcher with mocked linear_client + metrics_store,
write real sentinel files under tmp_path/.claude/, call the actual
method, assert state transitions and side effects.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import Watcher
from app.core.watcher.watcher_signals import (
    forcestop_sentinel_path,
    kill_sentinel_path,
    pause_sentinel_path,
    softstop_sentinel_path,
)
from app.core.watcher.watcher_types import ActiveWorker

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_manifest(**overrides: Any) -> ExecutionManifest:
    defaults: dict[str, Any] = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "review_mode": "auto",
        "base_branch": "main",
        "worker_branch": "wor-10-test",
        "objective": "Do thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)


def _make_active_worker(ticket_id: str = "WOR-X") -> ActiveWorker:
    manifest = _make_manifest(
        ticket_id=ticket_id,
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
    )
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-id",
        manifest=manifest,
        worktree_path=Path(f"/tmp/{ticket_id}"),
        process=MagicMock(spec=subprocess.Popen),
    )


def _make_watcher(tmp_path: Path) -> Watcher:
    return Watcher(
        linear_client=MagicMock(),
        metrics_store=MagicMock(),
        repo_root=tmp_path,
    )


# ── _log_startup_banner ─────────────────────────────────────────────────────


def test_log_startup_banner_cloud_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """cloud mode emits a cloud-specific banner."""
    w = _make_watcher(tmp_path)
    w._mode = "cloud"
    with caplog.at_level(logging.INFO):
        w._log_startup_banner()
    assert any("mode=cloud" in r.message for r in caplog.records)


def test_log_startup_banner_local_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """local mode emits a local-specific banner."""
    w = _make_watcher(tmp_path)
    w._mode = "local"
    with caplog.at_level(logging.INFO):
        w._log_startup_banner()
    assert any("mode=local" in r.message for r in caplog.records)


# ── _check_softstop_sentinel ────────────────────────────────────────────────


def test_check_softstop_sentinel_sets_draining(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sentinel present + not yet draining → _draining flips True + warning logs."""
    w = _make_watcher(tmp_path)
    sentinel = softstop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    assert w._draining is False
    with caplog.at_level(logging.WARNING):
        w._check_softstop_sentinel()
    assert w._draining is True
    assert w._draining_since is not None
    assert any("Soft-stop requested" in r.message for r in caplog.records)


def test_check_softstop_sentinel_noop_when_already_draining(tmp_path: Path) -> None:
    """Already draining → return immediately, _draining_since unchanged."""
    w = _make_watcher(tmp_path)
    w._draining = True
    original_since = 1234.0  # sentinel value
    w._draining_since = original_since

    sentinel = softstop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    w._check_softstop_sentinel()
    # Identity comparison — the method must not have written a new value
    # (avoids the floating-point-equality smell, S1244).
    assert w._draining_since is original_since


def test_check_softstop_sentinel_no_file_no_change(tmp_path: Path) -> None:
    """Sentinel absent → no state change."""
    w = _make_watcher(tmp_path)
    w._check_softstop_sentinel()
    assert w._draining is False


# ── _check_pause_sentinel + _resume ─────────────────────────────────────────


def test_check_pause_sentinel_sets_paused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pause sentinel present → _paused = True + warning logs."""
    w = _make_watcher(tmp_path)
    sentinel = pause_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    with caplog.at_level(logging.WARNING):
        w._check_pause_sentinel()
    assert w._paused is True
    assert any("Pause requested" in r.message for r in caplog.records)


def test_check_pause_sentinel_noop_when_already_paused(tmp_path: Path) -> None:
    """Already paused → early return."""
    w = _make_watcher(tmp_path)
    w._paused = True
    w._check_pause_sentinel()
    assert w._paused is True  # no state change attempted


def test_resume_clears_paused(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_resume flips _paused False + info-logs."""
    w = _make_watcher(tmp_path)
    w._paused = True
    with caplog.at_level(logging.INFO):
        w._resume()
    assert w._paused is False
    assert any("Pause cleared" in r.message for r in caplog.records)


def test_resume_noop_when_not_paused(tmp_path: Path) -> None:
    """Calling _resume when not paused is a no-op."""
    w = _make_watcher(tmp_path)
    w._paused = False
    w._resume()
    assert w._paused is False


# ── _check_forcestop_sentinel ───────────────────────────────────────────────


def test_check_forcestop_sentinel_commits_wip_and_terminates(tmp_path: Path) -> None:
    """Force-stop sentinel + 2 active workers → commit_wip_state called for each,
    process.terminate called, pools cleared, _paused set."""
    w = _make_watcher(tmp_path)
    worker_a = _make_active_worker(ticket_id="WOR-A")
    worker_b = _make_active_worker(ticket_id="WOR-B")
    w._local_active = [worker_a]
    w._cloud_active = [worker_b]

    sentinel = forcestop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    with patch("app.core.watcher.watcher.commit_wip_state") as mock_commit:
        w._check_forcestop_sentinel()

    assert w._forcestopping is True
    assert mock_commit.call_count == 2
    worker_a.process.terminate.assert_called_once()
    worker_b.process.terminate.assert_called_once()
    assert w._local_active == []
    assert w._cloud_active == []
    assert w._processed_tickets == []
    assert w._paused is True


def test_check_forcestop_sentinel_idempotent(tmp_path: Path) -> None:
    """Already forcestopping → return immediately, pools untouched."""
    w = _make_watcher(tmp_path)
    w._forcestopping = True
    worker = _make_active_worker(ticket_id="WOR-X")
    w._local_active = [worker]

    sentinel = forcestop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    w._check_forcestop_sentinel()
    assert w._local_active == [worker]  # untouched
    worker.process.terminate.assert_not_called()


def test_check_forcestop_sentinel_handles_terminate_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If process.terminate() raises OSError, the loop continues + warns."""
    w = _make_watcher(tmp_path)
    worker = _make_active_worker(ticket_id="WOR-T")
    worker.process.terminate.side_effect = OSError("nope")
    w._local_active = [worker]

    sentinel = forcestop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    with (
        patch("app.core.watcher.watcher.commit_wip_state"),
        caplog.at_level(logging.WARNING),
    ):
        w._check_forcestop_sentinel()

    assert any("Force-stop: failed to terminate" in r.message for r in caplog.records)
    assert w._local_active == []  # still cleared


# ── _check_kill_sentinel ────────────────────────────────────────────────────


def test_check_kill_sentinel_terminates_matching_worker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Kill sentinel with one matching ticket → process.terminate called."""
    w = _make_watcher(tmp_path)
    worker_match = _make_active_worker(ticket_id="WOR-K")
    worker_other = _make_active_worker(ticket_id="WOR-O")
    w._local_active = [worker_match, worker_other]

    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-K\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        w._check_kill_sentinel()

    assert not sentinel.exists()  # sentinel removed
    worker_match.process.terminate.assert_called_once()
    worker_other.process.terminate.assert_not_called()


def test_check_kill_sentinel_skips_missing_ticket(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ticket ID not among active workers → info-logs but doesn't crash."""
    w = _make_watcher(tmp_path)
    worker = _make_active_worker(ticket_id="WOR-Y")
    w._local_active = [worker]

    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-DOES-NOT-EXIST\n", encoding="utf-8")

    with caplog.at_level(logging.INFO):
        w._check_kill_sentinel()

    assert not sentinel.exists()
    worker.process.terminate.assert_not_called()
    assert any("not found among active workers" in r.message for r in caplog.records)


def test_check_kill_sentinel_no_file_noop(tmp_path: Path) -> None:
    """No sentinel → early return, no exceptions."""
    w = _make_watcher(tmp_path)
    w._check_kill_sentinel()  # should not raise


def test_check_kill_sentinel_empty_file_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sentinel exists but contains no ticket IDs → warning, sentinel removed."""
    w = _make_watcher(tmp_path)
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        w._check_kill_sentinel()

    assert not sentinel.exists()
    assert any("Kill sentinel was empty" in r.message for r in caplog.records)


def test_check_kill_sentinel_handles_terminate_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """terminate() raising OSError is caught + warning emitted."""
    w = _make_watcher(tmp_path)
    worker = _make_active_worker(ticket_id="WOR-FAIL")
    worker.process.terminate.side_effect = ValueError("borked")
    w._local_active = [worker]

    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-FAIL\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        w._check_kill_sentinel()

    assert any("Failed to terminate" in r.message for r in caplog.records)


# ── _poll_iteration — drain-complete exit path ─────────────────────────────


def test_poll_iteration_drain_complete_exits(tmp_path: Path) -> None:
    """When draining=True and pools are empty, _poll_iteration sets
    _running=False and removes the softstop sentinel."""
    w = _make_watcher(tmp_path)
    w._draining = True
    # Write the sentinel that should be removed
    sentinel = softstop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    with (
        patch.object(w, "_check_softstop_sentinel"),
        patch.object(w, "_check_pause_sentinel"),
        patch.object(w, "_check_forcestop_sentinel"),
        patch.object(w, "_check_kill_sentinel"),
        patch.object(w, "_reap_pool"),
        patch.object(w, "_retry_pending_sonar"),
        patch.object(w, "_promote_waiting_tickets"),
        patch.object(w, "_dispatch_next_ticket"),
        patch.object(w, "_check_epic_completion"),
        patch("app.core.watcher.watcher.terminate_overrun_workers"),
    ):
        result = w._poll_iteration()

    assert result is False
    assert w._running is False
    assert not sentinel.exists()


def test_poll_iteration_pause_gates_dispatch(tmp_path: Path) -> None:
    """When paused, the iteration does not call promote/dispatch/check_epic."""
    w = _make_watcher(tmp_path)
    w._paused = True

    with (
        patch.object(w, "_check_softstop_sentinel"),
        patch.object(w, "_check_pause_sentinel"),
        patch.object(w, "_check_forcestop_sentinel"),
        patch.object(w, "_check_kill_sentinel"),
        patch.object(w, "_reap_pool"),
        patch.object(w, "_retry_pending_sonar"),
        patch.object(w, "_promote_waiting_tickets") as mock_promote,
        patch.object(w, "_dispatch_next_ticket") as mock_dispatch,
        patch.object(w, "_check_epic_completion") as mock_check_epic,
        patch("app.core.watcher.watcher.terminate_overrun_workers"),
    ):
        w._poll_iteration()

    mock_promote.assert_not_called()
    mock_dispatch.assert_not_called()
    mock_check_epic.assert_not_called()


# ── _finalize_run ───────────────────────────────────────────────────────────


def test_finalize_run_calls_teardown(tmp_path: Path) -> None:
    """_finalize_run drives wait_for_active_workers + services.stop + pid removal."""
    w = _make_watcher(tmp_path)
    with (
        patch("app.core.watcher.watcher.wait_for_active_workers") as mock_wait,
        patch("app.core.watcher.watcher.remove_pid_file") as mock_rm,
    ):
        w._services = MagicMock()
        w._finalize_run()
    mock_wait.assert_called_once_with(w._local_active, w._cloud_active)
    w._services.stop.assert_called_once()
    mock_rm.assert_called_once()


# ── _emit_post_iteration_signals ────────────────────────────────────────────


def test_emit_post_iteration_signals_updates_idle_state(tmp_path: Path) -> None:
    """When emit_idle_line returns a state, _last_idle_state is updated."""
    w = _make_watcher(tmp_path)
    with (
        patch("app.core.watcher.watcher.emit_idle_line", return_value=(0, 0, 0, True)),
        patch("app.core.watcher.watcher.emit_heartbeat", return_value={}),
        patch("app.core.watcher.watcher.maybe_warn_softstop_stuck", return_value=False),
    ):
        w._emit_post_iteration_signals()
    assert w._last_idle_state == (0, 0, 0, True)


def test_emit_post_iteration_signals_preserves_idle_when_none(tmp_path: Path) -> None:
    """When emit_idle_line returns None (state unchanged), _last_idle_state stays."""
    w = _make_watcher(tmp_path)
    w._last_idle_state = (5, 0, 0, True)
    with (
        patch("app.core.watcher.watcher.emit_idle_line", return_value=None),
        patch("app.core.watcher.watcher.emit_heartbeat", return_value={}),
        patch("app.core.watcher.watcher.maybe_warn_softstop_stuck", return_value=False),
    ):
        w._emit_post_iteration_signals()
    assert w._last_idle_state == (5, 0, 0, True)


def test_emit_post_iteration_signals_records_softstop_warning(tmp_path: Path) -> None:
    """When maybe_warn_softstop_stuck returns True, the flag is set so we
    don't warn again next cycle."""
    w = _make_watcher(tmp_path)
    assert w._softstop_warned_stuck is False
    with (
        patch("app.core.watcher.watcher.emit_idle_line", return_value=None),
        patch("app.core.watcher.watcher.emit_heartbeat", return_value={}),
        patch("app.core.watcher.watcher.maybe_warn_softstop_stuck", return_value=True),
    ):
        w._emit_post_iteration_signals()
    assert w._softstop_warned_stuck is True


def test_emit_post_iteration_signals_forwards_heartbeat(tmp_path: Path) -> None:
    """emit_heartbeat's return value replaces w._heartbeat."""
    w = _make_watcher(tmp_path)
    new_hb = {"WOR-1": (30.0, 1)}
    with (
        patch("app.core.watcher.watcher.emit_idle_line", return_value=None),
        patch("app.core.watcher.watcher.emit_heartbeat", return_value=new_hb),
        patch("app.core.watcher.watcher.maybe_warn_softstop_stuck", return_value=False),
    ):
        w._emit_post_iteration_signals()
    assert w._heartbeat == new_hb


def test_finalize_run_stops_display_when_present(tmp_path: Path) -> None:
    """If TUI display is attached, _finalize_run calls display.stop()."""
    w = _make_watcher(tmp_path)
    w._display = MagicMock()
    with (
        patch("app.core.watcher.watcher.wait_for_active_workers"),
        patch("app.core.watcher.watcher.remove_pid_file"),
    ):
        w._services = MagicMock()
        w._finalize_run()
    w._display.stop.assert_called_once()
