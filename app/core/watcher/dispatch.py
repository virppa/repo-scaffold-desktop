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
from .watcher_helpers import (
    check_allowed_paths_overlap,
    resolve_effective_mode,
)
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
    project_id: str,
    services: ServiceManager,
    verbose: bool,
    retry_counters: dict[str, int],
    _local_active: list[ActiveWorker],
    _cloud_active: list[ActiveWorker],
    max_local_workers: int,
    max_cloud_workers: int,
    _repo_root: Path,
    _processed_tickets: list[object],
    linear_id: str,
    ticket_id: str,
    _escalation_policy: object,
) -> None:
    """Execute the full ticket-start flow extracted from Watcher._start_ticket."""
    open_blockers = linear.get_open_blockers(linear_id)
    if open_blockers:
        logger.info("Skipping %s — open blockers: %s", ticket_id, open_blockers)
        return

    all_active = _local_active + _cloud_active
    conflicts = check_allowed_paths_overlap(all_active, manifest)
    if conflicts:
        logger.info(
            "Deferring %s — allowed_paths overlap with active workers: %s",
            ticket_id,
            conflicts,
        )
        return

    effective_mode = resolve_effective_mode(
        services._mode if hasattr(services, "_mode") else "local",
        manifest.implementation_mode,
    )

    if effective_mode == "local":
        if len(_local_active) >= max_local_workers:
            logger.info(
                "Deferring %s — local pool full (%d/%d)",
                ticket_id,
                len(_local_active),
                max_local_workers,
            )
            return
    else:
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
            logger.warning("Deferring %s — vLLM not ready yet", ticket_id)
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
        _repo_root, manifest, worktree_path, effective_mode, verbose
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
