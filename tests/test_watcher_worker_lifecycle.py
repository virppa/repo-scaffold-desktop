"""Tests for the watcher worker lifecycle — finalize, reap pool, and
stuck-worker detection.

Covers ghost-slot leak prevention (WOR-334), soft-stop/drain (WOR-333), and
heartbeat-based stuck-worker detection (WOR-381, WOR-382).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time as _time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import (
    Watcher,
)
from app.core.watcher.watcher_heartbeat import emit_heartbeat
from app.core.watcher.watcher_signals import (
    _WORKER_HEARTBEAT_TIMEOUT_SECONDS,
    terminate_overrun_workers,
)
from app.core.watcher.watcher_types import ActiveWorker

# ---------------------------------------------------------------------------
# Fixtures / helpers
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
# WOR-381 — heartbeat-based stuck-worker detection (SIGTERM, then SIGKILL)
# ---------------------------------------------------------------------------


def _stuck_worker(
    tmp_path: Path,
    ticket_id: str = "WOR-99",
    *,
    log_idle_secs: float | None = None,
    terminated_at: float | None = None,
) -> ActiveWorker:
    """ActiveWorker with a worker log whose mtime is N seconds in the past.

    If `log_idle_secs` is None, no log file is created (simulates a freshly-
    dispatched worker that hasn't written anything yet).
    """
    worktree = tmp_path / ticket_id
    worktree.mkdir(parents=True, exist_ok=True)
    log_path = worktree / ".claude" / f"worker_{ticket_id.lower()}.log"
    if log_idle_secs is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("dummy stream-json line\n", encoding="utf-8")
        target_mtime = _time.time() - log_idle_secs
        os.utime(log_path, (target_mtime, target_mtime))

    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None  # still running
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id=f"linear-{ticket_id}",
        manifest=ExecutionManifest(
            **{
                "ticket_id": ticket_id,
                "epic_id": "WOR-96",
                "title": "Test ticket",
                "priority": 2,
                "status": "ReadyForLocal",
                "parallel_safe": True,
                "risk_level": "low",
                "implementation_mode": "local",
                "review_mode": "auto",
                "base_branch": "wor-96-local-worker-engine",
                "worker_branch": f"wor-{ticket_id.lower().replace('-', '')}-branch",
                "objective": "Do the thing.",
                "artifact_paths": ArtifactPaths.from_ticket_id(ticket_id),
                "allowed_paths": ["app/core/bar.py"],
                "required_checks": ["pytest"],
            }
        ),
        worktree_path=worktree,
        process=proc,
        terminated_at=terminated_at,
    )


def test_terminate_overrun_no_op_when_log_fresh(tmp_path: Path) -> None:
    """Log written 5 min ago is left alone (under heartbeat threshold)."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(tmp_path, log_idle_secs=5 * 60)
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.terminate.assert_not_called()
    worker.process.kill.assert_not_called()
    assert worker.terminated_at is None


def test_terminate_overrun_no_op_below_threshold_with_long_decode(
    tmp_path: Path,
) -> None:
    """WOR-388 regression: a worker silent for 30 min (long extended-thinking decode)
    is NOT killed under the post-WOR-388 90-min threshold. Pre-WOR-388 (15-min cap)
    this would have triggered SIGTERM and lost the legitimate work — exactly the
    failure mode that destroyed WOR-369 + WOR-362 on the WOR-383 batch."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    # 30 min idle: well past the old 15-min threshold, well under the new 90-min one.
    worker = _stuck_worker(tmp_path, log_idle_secs=30 * 60)
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.terminate.assert_not_called()
    worker.process.kill.assert_not_called()
    assert worker.terminated_at is None


def test_terminate_overrun_no_op_when_log_missing(tmp_path: Path) -> None:
    """Freshly-dispatched worker (no log yet) is not signalled."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(tmp_path, log_idle_secs=None)
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.terminate.assert_not_called()
    worker.process.kill.assert_not_called()


def test_terminate_overrun_sigterm_when_log_idle_past_threshold(tmp_path: Path) -> None:
    """Log idle > 15 min triggers SIGTERM."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path, log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60
    )
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.terminate.assert_called_once()
    worker.process.kill.assert_not_called()
    assert worker.terminated_at is not None


def test_terminate_overrun_sigterm_only_once(tmp_path: Path) -> None:
    """Repeated calls do not re-SIGTERM after the first one."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path, log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60
    )
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)
    first_terminated_at = worker.terminated_at
    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.terminate.assert_called_once()
    assert worker.terminated_at == first_terminated_at


def test_terminate_overrun_sigkill_after_grace(tmp_path: Path) -> None:
    """Worker still alive 6 min after SIGTERM is SIGKILL'd."""
    linear = MagicMock()
    w = Watcher(linear_client=linear, repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path,
        log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60,
        terminated_at=_time.time() - 6 * 60,
    )
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.kill.assert_called_once()
    linear.post_comment.assert_called_once()
    body = linear.post_comment.call_args[0][1]
    assert "stalled" in body
    assert "SIGTERM" in body
    assert "SIGKILL" in body


def test_terminate_overrun_no_sigkill_within_grace(tmp_path: Path) -> None:
    """Worker still alive <5 min after SIGTERM gets more grace."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path,
        log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60,
        terminated_at=_time.time() - 2 * 60,
    )
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.kill.assert_not_called()


def test_terminate_overrun_skips_already_exited(tmp_path: Path) -> None:
    """A worker whose process has exited (poll != None) is not signalled."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path, log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60
    )
    worker.process.poll.return_value = 0
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    worker.process.terminate.assert_not_called()
    worker.process.kill.assert_not_called()


def test_terminate_overrun_handles_terminate_oserror(tmp_path: Path) -> None:
    """OSError from terminate() doesn't loop us into retrying."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path, log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60
    )
    worker.process.terminate.side_effect = OSError("no such process")
    w._local_active.append(worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    assert worker.terminated_at is not None


def test_terminate_overrun_covers_both_pools(tmp_path: Path) -> None:
    """Both _local_active and _cloud_active are scanned."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    local_worker = _stuck_worker(
        tmp_path,
        ticket_id="WOR-100",
        log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60,
    )
    cloud_worker = _stuck_worker(
        tmp_path,
        ticket_id="WOR-101",
        log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60,
    )
    w._local_active.append(local_worker)
    w._cloud_active.append(cloud_worker)

    terminate_overrun_workers(w._local_active, w._cloud_active, w._linear)

    local_worker.process.terminate.assert_called_once()
    cloud_worker.process.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# wait_for_active_workers — no active workers (fast path)
# ---------------------------------------------------------------------------


def test_wait_for_active_workers_no_active_workers() -> None:
    """When there are no active workers, wait_for_active_workers must return
    immediately without calling process.wait()."""
    from app.core.watcher.watcher_signals import wait_for_active_workers

    wait_for_active_workers([], [])

    # No crash, no calls — just returns early


# ---------------------------------------------------------------------------
# wait_for_active_workers — normal exit (workers finish within timeout)
# ---------------------------------------------------------------------------


def test_wait_for_active_workers_normal_exit() -> None:
    """Workers that exit normally within the 600s timeout should be waited
    on and reaped cleanly."""
    from app.core.watcher.watcher_signals import wait_for_active_workers

    local_worker = _make_active_worker(ticket_id="WOR-LOCAL")
    cloud_worker = _make_active_worker(ticket_id="WOR-CLOUD")

    wait_for_active_workers([local_worker], [cloud_worker])

    # Both workers' process.wait() must have been called
    local_worker.process.wait.assert_called_once_with(timeout=600)
    cloud_worker.process.wait.assert_called_once_with(timeout=600)


# ---------------------------------------------------------------------------
# _emit_heartbeat — empty active list is no-op
# ---------------------------------------------------------------------------


def test_emit_heartbeat_no_active_workers() -> None:
    """When there are no active workers, _emit_heartbeat must not log."""
    w = Watcher(linear_client=MagicMock(), repo_root=Path("/tmp"))
    w._local_active.clear()
    w._cloud_active.clear()

    emit_heartbeat(
        w._local_active,
        w._cloud_active,
        w._heartbeat,
    )


# ---------------------------------------------------------------------------
# _emit_heartbeat — single local worker emits elapsed time
# ---------------------------------------------------------------------------


def test_emit_heartbeat_local_worker_emits_after_30s() -> None:
    """A worker that has been running for >30s should emit an elapsed-time
    log message. The first emission starts at the first 30-second boundary."""
    w = Watcher(linear_client=MagicMock(), repo_root=Path("/tmp"))
    worker = _make_active_worker(ticket_id="WOR-HEART")
    # Backdate start_time so elapsed > 30s
    worker.start_time = _time.monotonic() - 45
    w._local_active.append(worker)

    emit_heartbeat(
        w._local_active,
        w._cloud_active,
        w._heartbeat,
    )

    # Should have populated the heartbeat dict
    assert "WOR-HEART" in w._heartbeat


# ---------------------------------------------------------------------------
# _emit_heartbeat — tick not incremented stays silent
# ---------------------------------------------------------------------------


def test_emit_heartbeat_tick_boundary_no_duplicate() -> None:
    """When the worker's elapsed time hasn't crossed a new 30-second
    boundary since the last heartbeat, the method must be a no-op."""
    w = Watcher(linear_client=MagicMock(), repo_root=Path("/tmp"))
    worker = _make_active_worker(ticket_id="WOR-BND")
    worker.start_time = _time.monotonic() - 70  # 70s elapsed
    w._local_active.append(worker)
    w._heartbeat["WOR-BND"] = (70, 2)  # already ticked at 30s boundary

    emit_heartbeat(
        w._local_active,
        w._cloud_active,
        w._heartbeat,
    )

    # Heartbeat should still be at tick 2 (no crossing to 3 at 70s)
    assert w._heartbeat["WOR-BND"][1] == 2


# ---------------------------------------------------------------------------
# WOR-132: _retry_pending_sonar — deferred SonarCloud fetch retry
# ---------------------------------------------------------------------------


def test_retry_pending_sonar_success_on_first_poll(tmp_path: Path) -> None:
    """When the pending worker is found, findings are fetched and metrics
    are backfilled, then the worker is removed from the pending set."""
    w = Watcher(repo_root=tmp_path, metrics_store=MagicMock())
    w._project_id = "test-proj"
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-branch")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    worker.pending_sonar_fetch = True
    worker.sonar_fetch_attempts = 1
    w._pending_sonar_workers["WOR-10"] = worker
    w._metrics.get_by_ticket = MagicMock(return_value=None)

    with patch(
        "app.core.watcher.watcher.fetch_sonar_findings",
        return_value=["BLOCKER", "CRITICAL"],
    ):
        w._retry_pending_sonar()

    w._metrics.update_sonar_count.assert_called_once_with("WOR-10", "test-proj", 2)
    assert "WOR-10" not in w._pending_sonar_workers


def test_retry_pending_sonar_exhausts_budget(tmp_path: Path) -> None:
    """After 3 attempts the worker is removed from the pending set without
    updating metrics."""
    w = Watcher(repo_root=tmp_path, metrics_store=MagicMock())
    w._project_id = "test-proj"
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-branch")
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    worker.pending_sonar_fetch = True
    worker.sonar_fetch_attempts = 3  # already at budget
    w._pending_sonar_workers["WOR-10"] = worker
    w._metrics.get_by_ticket = MagicMock(return_value=None)

    with patch(
        "app.core.watcher.watcher.fetch_sonar_findings",
        return_value=None,
    ):
        w._retry_pending_sonar()

    w._metrics.update_sonar_count.assert_not_called()
    assert "WOR-10" not in w._pending_sonar_workers


def test_retry_pending_sonar_empty_set_noop(tmp_path: Path) -> None:
    """When there are no pending workers, the method returns without error."""
    w = Watcher(repo_root=tmp_path, metrics_store=MagicMock())
    w._project_id = "test-proj"
    assert w._pending_sonar_workers == {}
    w._retry_pending_sonar()  # Should not raise
    w._metrics.update_sonar_count.assert_not_called()
