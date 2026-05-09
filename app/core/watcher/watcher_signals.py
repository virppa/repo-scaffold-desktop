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

from app.core.watcher.watcher_types import _PID_FILE, _WORKTREE_BASE

if TYPE_CHECKING:
    from app.core.watcher.watcher_types import ActiveWorker

logger = logging.getLogger(__name__)

_SOFTSTOP_SENTINEL_NAME = "watcher.softstop"
_SOFTSTOP_WARN_AFTER_MIN = 60


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _handle_signal_impl(
    services: Any,
    running_ref: Any,
    signum: int,
    frame: Any,
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
    return lambda signum, frame: _handle_signal_impl(
        services, running_ref, signum, frame
    )


# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------


def write_pid_file(repo_root: Path) -> None:
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
            logger.warning("Could not remove stale sentinel %s: %s", sentinel, exc)


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
    should be set afterwards), ``False`` otherwise.
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
