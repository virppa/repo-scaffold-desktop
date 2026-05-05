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
    _WORKER_HEARTBEAT_TIMEOUT_SECONDS,
    Watcher,
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

    w._terminate_overrun_workers()

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

    w._terminate_overrun_workers()

    worker.process.terminate.assert_not_called()
    worker.process.kill.assert_not_called()
    assert worker.terminated_at is None


def test_terminate_overrun_no_op_when_log_missing(tmp_path: Path) -> None:
    """Freshly-dispatched worker (no log yet) is not signalled."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(tmp_path, log_idle_secs=None)
    w._local_active.append(worker)

    w._terminate_overrun_workers()

    worker.process.terminate.assert_not_called()
    worker.process.kill.assert_not_called()


def test_terminate_overrun_sigterm_when_log_idle_past_threshold(tmp_path: Path) -> None:
    """Log idle > 15 min triggers SIGTERM."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path, log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60
    )
    w._local_active.append(worker)

    w._terminate_overrun_workers()

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

    w._terminate_overrun_workers()
    first_terminated_at = worker.terminated_at
    w._terminate_overrun_workers()

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

    w._terminate_overrun_workers()

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

    w._terminate_overrun_workers()

    worker.process.kill.assert_not_called()


def test_terminate_overrun_skips_already_exited(tmp_path: Path) -> None:
    """A worker whose process has exited (poll != None) is not signalled."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    worker = _stuck_worker(
        tmp_path, log_idle_secs=_WORKER_HEARTBEAT_TIMEOUT_SECONDS + 60
    )
    worker.process.poll.return_value = 0
    w._local_active.append(worker)

    w._terminate_overrun_workers()

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

    w._terminate_overrun_workers()

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

    w._terminate_overrun_workers()

    local_worker.process.terminate.assert_called_once()
    cloud_worker.process.terminate.assert_called_once()
