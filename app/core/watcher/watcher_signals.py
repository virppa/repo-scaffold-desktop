"""Signal handling, PID lifecycle, soft-stop, and stuck-worker detection.

Extracted from ``watcher.py`` (WOR-401). Module-level functions for
signal dispatch, PID file ops, soft-stop sentinel ops, and stuck-worker
detection. The ``Watcher`` class delegates to these functions.
"""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.core.watcher.watcher_types import (
    _CLAUDE_DIR,
    _PID_FILE,
    _WATCHER_FORCESTOP_SENTINEL_NAME,
    _WATCHER_KILL_SENTINEL_NAME,
    _WATCHER_PAUSE_SENTINEL_NAME,
    _WORKTREE_BASE,
    LinearClientProtocol,
)

if TYPE_CHECKING:
    from app.core.watcher.watcher_types import ActiveWorker

logger = logging.getLogger(__name__)

# Sonar S1192: shared warning template — every stale-sentinel cleanup path
# logs the same message when unlink fails, so consolidate the literal here.
_STALE_SENTINEL_WARN_MSG = "Could not remove stale sentinel %s: %s"

_SOFTSTOP_SENTINEL_NAME = "watcher.softstop"
_SOFTSTOP_WARN_AFTER_MIN = 60


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _handle_signal_impl(
    services: Any,
    running_ref: Any,
    signum: int,
) -> None:
    """Actual signal handler logic — called with (signum, frame) by the signal
    module. ``services`` and ``running_ref`` are captured by the closure in
    ``Watcher._handle_signal``."""
    logger.info("Signal %d received — finishing active workers then exiting", signum)
    services.stop()
    running_ref._running = False


def make_signal_handler(
    services: object,
    running_ref: object,
) -> Callable[[int, Any], None]:
    """Factory that creates a ``(signum, frame)`` handler bound to ``self``."""
    return lambda signum, _frame: _handle_signal_impl(services, running_ref, signum)


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------


def write_pid_file() -> None:
    """Write the watcher PID to ``<repo>/.claude/watcher.pid``."""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid_file() -> None:
    """Remove the watcher PID file if it exists."""
    try:
        _PID_FILE.unlink()
    except FileNotFoundError:
        pass


def cleanup_orphaned_worktrees(
    repo_root: Path,
    cleanup_worktree: Callable[[Path, Path], None],
) -> None:
    """Scan the worktrees base directory and clean up orphans."""
    base = repo_root.parent / _WORKTREE_BASE
    if not base.exists():
        return
    for worktree_dir in base.iterdir():
        if not worktree_dir.is_dir():
            continue
        logger.warning("Orphaned worktree detected: %s — removing", worktree_dir)
        cleanup_worktree(repo_root, worktree_dir)


# ---------------------------------------------------------------------------
# Worker wait
# ---------------------------------------------------------------------------


def wait_for_active_workers(
    local_active: list[ActiveWorker],
    cloud_active: list[ActiveWorker],
) -> None:
    """Wait for all active workers to finish (or timeout)."""
    all_active: list[ActiveWorker] = []
    all_active.extend(local_active)
    all_active.extend(cloud_active)
    if not all_active:
        return
    logger.info("Waiting for %d active worker(s) to finish…", len(all_active))
    for worker in all_active:
        try:
            worker.process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            logger.warning("Worker %s timed out — terminating", worker.ticket_id)
            worker.process.terminate()


# ---------------------------------------------------------------------------
# Soft-stop sentinel path
# ---------------------------------------------------------------------------


def softstop_sentinel_path(repo_root: Path) -> Path:
    """Return the path of the soft-stop sentinel file."""
    return repo_root / _PID_FILE.parent / _SOFTSTOP_SENTINEL_NAME


# ---------------------------------------------------------------------------
# Soft-stop / drain mode (WOR-333)
# ---------------------------------------------------------------------------


def remove_stale_softstop_sentinel(repo_root: Path) -> None:
    """Delete any sentinel left over from a prior daemon run."""
    sentinel = softstop_sentinel_path(repo_root)
    if sentinel.exists():
        try:
            sentinel.unlink()
            logger.info("Removed stale soft-stop sentinel from prior run: %s", sentinel)
        except OSError as exc:
            logger.warning(_STALE_SENTINEL_WARN_MSG, sentinel, exc)


def remove_softstop_sentinel(repo_root: Path) -> None:
    """Delete the sentinel during graceful drain exit."""
    sentinel = softstop_sentinel_path(repo_root)
    try:
        sentinel.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove soft-stop sentinel %s: %s", sentinel, exc)


def maybe_warn_softstop_stuck(
    draining: bool,
    draining_since: float | None,
    softstop_warned_stuck: bool,
    local_active: list[ActiveWorker],
    cloud_active: list[ActiveWorker],
) -> bool:
    """Log a one-shot WARNING if drain has been pending too long.

    Returns ``True`` if the warning was logged (i.e. ``softstop_warned_stuck``
    should be afterwards), ``False`` otherwise.
    """
    if not draining:
        return False
    if softstop_warned_stuck:
        return False
    if draining_since is None:
        return False
    elapsed_min = (time.monotonic() - draining_since) / 60.0
    if elapsed_min < _SOFTSTOP_WARN_AFTER_MIN:
        return False
    active = local_active + cloud_active
    active_summary = ", ".join(
        f"{w.ticket_id} (running {(time.monotonic() - w.start_time) / 60:.0f}m)"
        for w in active
    )
    logger.warning(
        "Soft-stop pending for %.0f min. Worker(s) may be hung. "
        "Consider Ctrl-C to force-exit (will lose WIP). Active: %s",
        elapsed_min,
        active_summary or "(none — drain should have exited)",
    )
    return True


# ---------------------------------------------------------------------------
# Worker termination (WOR-381 + WOR-388 stuck-worker detection)
# ---------------------------------------------------------------------------
#
# Heartbeat-based: track when the worker's stream-json log was last written.
# A genuinely stuck worker (vLLM unresponsive, tool subprocess hung, infinite
# deadlock) stops appending lines, while a slow-but-progressing worker keeps
# writing. Threshold defaults to 90 min (raised from 15 min in WOR-388 after
# the original threshold killed legitimately-reasoning workers mid-decode);
# override at launch with WATCHER_WORKER_HEARTBEAT_TIMEOUT_SECONDS=N for tuning.

_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 90 * 60
_WORKER_HEARTBEAT_TIMEOUT_SECONDS = int(
    os.environ.get(
        "WATCHER_WORKER_HEARTBEAT_TIMEOUT_SECONDS",
        _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    )
)
_WORKER_KILL_GRACE_SECONDS = 5 * 60


def worker_log_idle(worker: "ActiveWorker", now_wall: float) -> float | None:
    """Return seconds since worker log last wrote, or None if log missing."""
    log_path = (
        worker.worktree_path / _CLAUDE_DIR / f"worker_{worker.ticket_id.lower()}.log"
    )
    try:
        return now_wall - log_path.stat().st_mtime
    except OSError:
        return None


def send_sigterm_if_stalled(
    worker: "ActiveWorker", idle_seconds: float, now_wall: float
) -> bool:
    """Stage 1: SIGTERM the worker if its log has been idle past threshold.

    Returns True if SIGTERM was issued (caller skips stage 2 this cycle).
    """
    if worker.terminated_at is not None:
        return False
    if idle_seconds <= _WORKER_HEARTBEAT_TIMEOUT_SECONDS:
        return False
    if worker.process.poll() is not None:
        return False
    logger.warning(
        "Worker %s heartbeat stalled - log idle for %.0fs "
        "(threshold %ds). Sending SIGTERM. SIGKILL grace: %ds.",
        worker.ticket_id,
        idle_seconds,
        _WORKER_HEARTBEAT_TIMEOUT_SECONDS,
        _WORKER_KILL_GRACE_SECONDS,
    )
    try:
        worker.process.terminate()
    except (OSError, ValueError) as exc:
        logger.warning("Failed to SIGTERM %s: %s", worker.ticket_id, exc)
    worker.terminated_at = now_wall
    return True


def send_sigkill_if_grace_expired(
    worker: "ActiveWorker",
    idle_seconds: float,
    now_wall: float,
    linear: LinearClientProtocol,
) -> None:
    """Stage 2: SIGKILL the worker if SIGTERM grace period has lapsed."""
    if worker.terminated_at is None:
        return
    if now_wall - worker.terminated_at <= _WORKER_KILL_GRACE_SECONDS:
        return
    if worker.process.poll() is not None:
        return
    logger.error(
        "Worker %s did not exit within %ds of SIGTERM - sending SIGKILL.",
        worker.ticket_id,
        _WORKER_KILL_GRACE_SECONDS,
    )
    try:
        worker.process.kill()
    except (OSError, ValueError) as exc:
        logger.warning("Failed to SIGKILL %s: %s", worker.ticket_id, exc)
    slug = worker.ticket_id.lower().replace("-", "_")
    try:
        linear.post_comment(
            worker.linear_id,
            (
                f"Worker stalled: log idle {int(idle_seconds)}s "
                f"(threshold {_WORKER_HEARTBEAT_TIMEOUT_SECONDS}s). "
                f"SIGTERM was sent, then SIGKILL after "
                f"{_WORKER_KILL_GRACE_SECONDS}s grace. The ticket "
                "will be marked Blocked by the natural failure "
                f"path. Inspect `.claude/artifacts/{slug}/` for "
                "the partial worker log."
            ),
        )
    except Exception as exc:
        logger.warning(
            "Could not post timeout comment for %s: %s",
            worker.ticket_id,
            exc,
        )


def terminate_overrun_workers(
    local_active: list["ActiveWorker"],
    cloud_active: list["ActiveWorker"],
    linear: LinearClientProtocol,
) -> None:
    """Heartbeat-based stuck-worker detection (WOR-381 + WOR-388).

    See module-level constants for the threshold/grace tuning history.
    """
    now_wall = time.time()
    for worker in (*local_active, *cloud_active):
        idle_seconds = worker_log_idle(worker, now_wall)
        if idle_seconds is None:
            continue
        if send_sigterm_if_stalled(worker, idle_seconds, now_wall):
            continue
        send_sigkill_if_grace_expired(worker, idle_seconds, now_wall, linear)


# ---------------------------------------------------------------------------
# Force-stop sentinel path
# ---------------------------------------------------------------------------


def forcestop_sentinel_path(repo_root: Path) -> Path:
    """Return the path of the force-stop sentinel file."""
    return repo_root / _PID_FILE.parent / _WATCHER_FORCESTOP_SENTINEL_NAME


def remove_stale_forcestop_sentinel(repo_root: Path) -> None:
    """Delete any sentinel left over from a prior daemon run."""
    sentinel = forcestop_sentinel_path(repo_root)
    if sentinel.exists():
        try:
            sentinel.unlink()
            logger.info(
                "Removed stale force-stop sentinel from prior run: %s", sentinel
            )
        except OSError as exc:
            logger.warning(_STALE_SENTINEL_WARN_MSG, sentinel, exc)


# ---------------------------------------------------------------------------
# Pause sentinel path
# ---------------------------------------------------------------------------


def pause_sentinel_path(repo_root: Path) -> Path:
    """Return the path of the pause sentinel file."""
    return repo_root / _PID_FILE.parent / _WATCHER_PAUSE_SENTINEL_NAME


def remove_stale_pause_sentinel(repo_root: Path) -> None:
    """Delete any sentinel left over from a prior daemon run."""
    sentinel = pause_sentinel_path(repo_root)
    if sentinel.exists():
        try:
            sentinel.unlink()
            logger.info("Removed stale pause sentinel from prior run: %s", sentinel)
        except OSError as exc:
            logger.warning(_STALE_SENTINEL_WARN_MSG, sentinel, exc)


# ---------------------------------------------------------------------------
# Kill sentinel path
# ---------------------------------------------------------------------------


def kill_sentinel_path(repo_root: Path) -> Path:
    """Return the path of the kill sentinel file."""
    return repo_root / _PID_FILE.parent / _WATCHER_KILL_SENTINEL_NAME


def remove_stale_kill_sentinel(repo_root: Path) -> None:
    """Delete any sentinel left over from a prior daemon run."""
    sentinel = kill_sentinel_path(repo_root)
    if sentinel.exists():
        try:
            sentinel.unlink()
            logger.info("Removed stale kill sentinel from prior run: %s", sentinel)
        except OSError as exc:
            logger.warning(_STALE_SENTINEL_WARN_MSG, sentinel, exc)


def read_kill_sentinel(repo_root: Path) -> list[str]:
    """Read ticket IDs from the kill sentinel file.

    Returns an empty list when the file does not exist or is empty.
    Ticket IDs are uppercased and blank lines are stripped.
    """
    sentinel = kill_sentinel_path(repo_root)
    if not sentinel.exists():
        return []
    try:
        content = sentinel.read_text(encoding="utf-8").strip()
        if not content:
            return []
        return [line.strip().upper() for line in content.splitlines() if line.strip()]
    except OSError:
        return []


def remove_kill_sentinel(repo_root: Path) -> None:
    """Remove the kill sentinel after it has been processed."""
    sentinel = kill_sentinel_path(repo_root)
    try:
        sentinel.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove kill sentinel %s: %s", sentinel, exc)
