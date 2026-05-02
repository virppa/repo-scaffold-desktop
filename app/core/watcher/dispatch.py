"""Dispatch functions for the watcher orchestrator.

Extracted from Watcher methods to reduce watcher.py LOC toward the
≤500 Recommend tier. Each function is a module-level callable that
receives all context it needs as explicit parameters — no class
instantiation required.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.manifest import ExecutionManifest

from .watcher_finalize import safe_set_state
from .watcher_helpers import resolve_effective_mode, suppress_dedup
from .watcher_services import ServiceManager
from .watcher_subprocess import launch_worker
from .watcher_types import ActiveWorker, LinearClientProtocol
from .watcher_worktrees import (
    backup_plan_files,
    copy_manifest_to_worktree,
    create_worktree,
    write_worker_pytest_config,
)

logger = logging.getLogger(__name__)


def start_ticket(
    manifest: ExecutionManifest,
    linear: LinearClientProtocol,
    services: ServiceManager,
    worker_verbose: bool,
    _local_active: list[ActiveWorker],
    _cloud_active: list[ActiveWorker],
    max_cloud_workers: int,
    _repo_root: Path,
    _processed_tickets: list[object],
    linear_id: str,
    ticket_id: str,
    _escalation_policy: object,
    _dedup_state: dict[str, str] | None = None,
) -> None:
    """Execute the full ticket-start flow extracted from Watcher._start_ticket."""
    # Prerequisite checks (open_blockers + overlap) are handled by
    # Watcher._start_ticket before calling this function.
    effective_mode = resolve_effective_mode(
        services._mode if hasattr(services, "_mode") else "local",
        manifest.implementation_mode,
    )

    if effective_mode != "local":
        if len(_cloud_active) >= max_cloud_workers:
            logger.info(
                "Deferring %s — cloud pool full (%d/%d)",
                ticket_id,
                len(_cloud_active),
                max_cloud_workers,
            )
            return

    if effective_mode == "local":
        if not services.probe_vllm_health():
            reason_msg = "Deferring %s — vLLM not ready yet" % (ticket_id,)
            if suppress_dedup(
                ticket_id, "vllm_not_ready", reason_msg, _dedup_state or {}
            ):
                logger.warning("%s", reason_msg)
            return
        services.ensure_litellm_running()

    worktree_path = create_worktree(_repo_root, manifest)
    copy_manifest_to_worktree(_repo_root, manifest, worktree_path)
    write_worker_pytest_config(worktree_path)

    safe_set_state(
        linear,
        linear_id,
        manifest.ticket_state_map.in_progress_local,
        ticket_id,
    )
    logger.info("Launching worker for %s (mode=%s)", ticket_id, effective_mode)

    backed_up_plans = backup_plan_files()
    process = launch_worker(
        _repo_root, manifest, worktree_path, effective_mode, worker_verbose
    )
    worker = ActiveWorker(
        ticket_id=ticket_id,
        linear_id=linear_id,
        manifest=manifest,
        worktree_path=worktree_path,
        process=process,
        backed_up_plans=backed_up_plans,
    )
    if effective_mode == "local":
        _local_active.append(worker)
    else:
        _cloud_active.append(worker)
