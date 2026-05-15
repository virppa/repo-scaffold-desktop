"""Heartbeat, idle-line, and TUI state functions extracted from watcher.py.

Extracted from ``watcher.py`` (WOR-414). Module-level functions replace the
corresponding ``Watcher`` instance methods. Each function takes only the state
it needs as arguments — no ``self`` references, no class imports.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.manifest import ExecutionManifest
from app.core.metrics import CostRollup
from app.core.watcher.watcher_log_parsing import (
    _parse_worker_usage,
    format_elapsed,
    last_tool_call,
)
from app.core.watcher.watcher_tui import QueueState, TrackedPR, TUIState, WorkerState

if TYPE_CHECKING:
    from app.core.metrics import MetricsStore
    from app.core.watcher.watcher_types import ActiveWorker, LinearClientProtocol

# Estimated cost per input/output token for local workers (sonnet-4-6 pricing).
# Used to produce a live cost estimate from the JSONL token counts during running.
_LOCAL_COST_PER_INPUT_TOKEN = 3e-6  # $3 per million input tokens
_LOCAL_COST_PER_OUTPUT_TOKEN = 15e-6  # $15 per million output tokens


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLAUDE_DIR = ".claude"
_ARTIFACTS_DIR = "artifacts"
_MANIFEST_GLOB = "*/manifest.json"


# ---------------------------------------------------------------------------
# Idle-line emission
# ---------------------------------------------------------------------------


def emit_idle_line(
    now_local: int,
    now_cloud: int,
    max_local_workers: int,
    max_cloud_workers: int,
    repo_root: Path,
    last_idle_state: tuple[int, int, int, bool] | None,
) -> tuple[int, int, int, bool] | None:
    """Emit a single idle line when the watcher has nothing active to do.

    Only re-emits when state changes (pool sizes / waiting count / capacity).

    Returns the new idle state, or ``None`` if no emission occurred (state
    unchanged from previous call).
    """
    has_local = now_local < max_local_workers
    has_cloud = now_cloud < max_cloud_workers
    has_capacity = has_local or has_cloud

    # Count WaitingForDeps manifests
    artifacts_root = repo_root / _CLAUDE_DIR / _ARTIFACTS_DIR
    waiting = 0
    if artifacts_root.exists():
        for mp in artifacts_root.glob(_MANIFEST_GLOB):
            try:
                m = ExecutionManifest.from_json(mp)
            except (OSError, ValueError):
                continue
            if m.status == "WaitingForDeps":
                waiting += 1

    state = (now_local, now_cloud, waiting, has_capacity)
    if state == last_idle_state:
        return None

    logger.info(
        "Watcher idle — %d/%d local, %d/%d cloud, %d waiting for blockers, "
        "polling every %ds",
        now_local,
        max_local_workers,
        now_cloud,
        max_cloud_workers,
        waiting,
        10,  # POLL_INTERVAL
    )
    return state


# ---------------------------------------------------------------------------
# Heartbeat emission
# ---------------------------------------------------------------------------


def emit_heartbeat(
    local_active: list["ActiveWorker"],
    cloud_active: list["ActiveWorker"],
    heartbeat: dict[str, tuple[float, int]],
) -> dict[str, tuple[float, int]]:
    """Emit a per-worker heartbeat every ~30s with elapsed time.

    Updates the ``heartbeat`` dict in-place and logs elapsed time for each
    worker that crosses a 30-second boundary.

    Returns the (possibly mutated) heartbeat dict.
    """
    all_active: list["ActiveWorker"] = []
    all_active.extend(local_active)
    all_active.extend(cloud_active)

    for worker in all_active:
        elapsed = time.monotonic() - worker.start_time
        key = worker.ticket_id

        if key in heartbeat:
            _, last_tick = heartbeat[key]
            # Emit when we cross a new 30-second boundary
            new_tick = int(elapsed / 30)
            if new_tick <= last_tick:
                continue
            heartbeat[key] = (elapsed, new_tick)
        else:
            # First emission — start at the first 30-second boundary
            tick = int(elapsed / 30)
            if tick < 1:
                continue
            heartbeat[key] = (elapsed, tick)

        elapsed_str = format_elapsed(elapsed)
        logger.info("[%s] %s", worker.ticket_id, elapsed_str)

    return heartbeat


# ---------------------------------------------------------------------------
# TUI state
# ---------------------------------------------------------------------------


def _build_local_worker_log_path(worker: "ActiveWorker") -> Path:
    """Construct the log path for a local worker's session."""
    return (
        worker.worktree_path
        / _CLAUDE_DIR
        / "logs"
        / f"{worker.ticket_id.replace('-', '_')}.jsonl"
    )


def _live_cost_estimate(input_tokens: int | None, output_tokens: int | None) -> float:
    """Estimate dollar cost from input + output token counts."""
    if input_tokens is None and output_tokens is None:
        return 0.0
    in_cost = (input_tokens or 0) * _LOCAL_COST_PER_INPUT_TOKEN
    out_cost = (output_tokens or 0) * _LOCAL_COST_PER_OUTPUT_TOKEN
    return in_cost + out_cost


def _count_queue_items(
    linear: "LinearClientProtocol",
    repo_root: Path,
) -> QueueState:
    """Count tickets in each queue bucket.

    ReadyForLocal and InProgressLocal come from Linear state queries.
    WaitingForDeps and Blocked come from manifest status scans.
    """
    try:
        ready = len(linear.list_issues_by_state("ReadyForLocal"))
    except Exception:
        ready = 0

    try:
        in_progress = len(linear.list_issues_by_state("InProgressLocal"))
    except Exception:
        in_progress = 0

    # Manifest-sourced: WaitingForDeps and Blocked.
    waiting = 0
    blocked = 0
    artifacts_root = repo_root / _CLAUDE_DIR / _ARTIFACTS_DIR
    if artifacts_root.exists():
        for mp in artifacts_root.glob(_MANIFEST_GLOB):
            try:
                m = ExecutionManifest.from_json(mp)
            except (OSError, ValueError):
                continue
            if m.status == "WaitingForDeps":
                waiting += 1
            # Blocked manifests are rare (manual intervention); but check.
            if m.status == "Blocked":
                blocked += 1

    return QueueState(
        ready=ready,
        waiting=waiting,
        in_progress=in_progress,
        blocked=blocked,
    )


def build_tui_state(
    local_active: list["ActiveWorker"],
    cloud_active: list["ActiveWorker"],
    metrics: MetricsStore,
    tracked_prs: list[TrackedPR],
    *,
    vllm_metrics: dict[str, float] | None = None,
    queue_state: QueueState | None = None,
) -> TUIState:
    """Build the current TUI snapshot for the display."""
    workers: list[WorkerState] = []
    for w in local_active:
        elapsed = time.monotonic() - w.start_time
        log_path = _build_local_worker_log_path(w)
        in_tok, out_tok, _, _ = _parse_worker_usage(log_path)
        cost = _live_cost_estimate(in_tok, out_tok)
        last_act = last_tool_call(log_path)
        workers.append(
            WorkerState(
                ticket_id=w.ticket_id,
                mode="local",
                status="running",
                elapsed_s=elapsed,
                local_saved=cost,
                last_action=last_act,
            )
        )
    for w in cloud_active:
        elapsed = time.monotonic() - w.start_time
        workers.append(
            WorkerState(
                ticket_id=w.ticket_id,
                mode="cloud",
                status="running",
                elapsed_s=elapsed,
            )
        )
    rollups: dict[str, CostRollup] = {}
    for period in ("today", "week", "all"):
        rollups[period] = metrics.get_cost_rollup(period)
    return TUIState(
        workers=workers,
        cost_rollups=rollups,
        tracked_prs=tracked_prs,
        vllm_metrics=vllm_metrics,
        queue_state=queue_state or QueueState(),
    )
