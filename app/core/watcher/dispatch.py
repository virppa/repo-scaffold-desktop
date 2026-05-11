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
    capture_vllm_metrics,
    count_main_ahead_of_epic,
    resolve_effective_mode,
    suppress_dedup,
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

# WOR-373: epic branches that lag main by more than this many commits are
# refused at dispatch. Long-lived epics silently shed main-side changes
# during periodic merge conflict resolution; the WOR-282 forensic uncovered
# 13 lost tests on a 107-commit-behind epic. 30 commits is roughly 3-7 days
# of drift in this repo; healthy epics stay well under it.
_EPIC_DRIFT_THRESHOLD = 30


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
    _dedup_state: dict[str, str],
) -> None:
    """Execute the full ticket-start flow extracted from Watcher._start_ticket."""
    # Prerequisite checks (open_blockers + overlap) are handled by
    # Watcher._start_ticket before calling this function.

    # WOR-419: defense-in-depth — refuse to spawn a worker on a new epic/*
    # branch when another epic/* branch is already in flight. This prevents
    # overlapping epic work from competing for the same shared files
    # (CLAUDE.md, templates/, etc.) and keeps the epic branch topology clean.
    # Gate fires ONLY when this ticket's base_branch is epic/* AND another
    # active worker is already on a different epic/* branch. Sub-ticket
    # branches under the same epic are unaffected — those are the normal
    # dispatch path and handled by the overlap check above.
    if manifest.base_branch.startswith("epic/"):
        for worker in _local_active:
            if (
                hasattr(worker, "manifest")
                and worker.manifest.base_branch.startswith("epic/")
                and worker.manifest.base_branch != manifest.base_branch
            ):
                logger.warning(
                    "Deferring %s — epic branch %s already in-flight (worker on %s)",
                    ticket_id,
                    manifest.base_branch,
                    worker.manifest.base_branch,
                )
                linear.post_comment(
                    linear_id,
                    (
                        f"Dispatch deferred: another worker is already "
                        f"in-flight on epic branch "
                        f"`{worker.manifest.base_branch}`. "
                        f"Cannot dispatch to a new epic branch "
                        f"`{manifest.base_branch}` until the in-flight "
                        f"worker completes (one-active-epic-branch "
                        f"principle, WOR-419)."
                    ),
                )
                return

    # Manifest quality gates (WOR-378) — Pydantic validation accepts these as
    # legal but they are almost certainly authoring mistakes. Refuse early
    # with a clear Linear comment so the operator knows to re-author.
    if manifest.implementation_mode == "local" and not manifest.allowed_paths:
        logger.warning(
            "Refusing %s — local manifest has empty allowed_paths", ticket_id
        )
        safe_set_state(linear, linear_id, "Backlog", ticket_id)
        linear.post_comment(
            linear_id,
            (
                "Manifest refused: `allowed_paths` is empty. Local worker "
                "dispatch requires explicit path scoping so the watcher can "
                "guarantee path-overlap parallelism. Re-run `/start-ticket` "
                "to populate the field."
            ),
        )
        return
    if not manifest.required_checks:
        logger.warning("Refusing %s — manifest has empty required_checks", ticket_id)
        safe_set_state(linear, linear_id, "Backlog", ticket_id)
        linear.post_comment(
            linear_id,
            (
                "Manifest refused: `required_checks` is empty. Worker must "
                "have at least one check (typically: `ruff check .`, "
                "`mypy app/`, `pytest`) before declaring success. Re-run "
                "`/start-ticket`."
            ),
        )
        return

    # WOR-373: stale-epic refusal. If the sub-ticket's base_branch is an
    # epic that has fallen too far behind main, block dispatch. Stale epics
    # silently drop main-side work during the next merge conflict resolution
    # (see WOR-282 forensic — 13 tests lost on a 107-commit-behind epic).
    drift = count_main_ahead_of_epic(manifest.base_branch, _repo_root)
    if drift > _EPIC_DRIFT_THRESHOLD:
        logger.warning(
            "Refusing %s — epic %s is %d commits behind origin/main (threshold %d)",
            ticket_id,
            manifest.base_branch,
            drift,
            _EPIC_DRIFT_THRESHOLD,
        )
        safe_set_state(linear, linear_id, "Backlog", ticket_id)
        linear.post_comment(
            linear_id,
            (
                f"Manifest refused: epic branch `{manifest.base_branch}` is "
                f"{drift} commits behind `origin/main` (threshold: "
                f"{_EPIC_DRIFT_THRESHOLD}). Stale epics silently drop tests "
                f"during merge conflict resolution — see WOR-282 forensic. "
                f"Merge main into the epic first, then re-dispatch:\n\n"
                f"  git fetch origin\n"
                f"  git checkout {manifest.base_branch}\n"
                f"  git merge origin/main\n"
                f"  # resolve conflicts...\n"
                f"  git push"
            ),
        )
        return

    effective_mode = resolve_effective_mode(
        services._mode if hasattr(services, "_mode") else "local",
        manifest.implementation_mode,
    )

    if effective_mode != "local" and len(_cloud_active) >= max_cloud_workers:
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
            if suppress_dedup(ticket_id, "vllm_not_ready", reason_msg, _dedup_state):
                logger.warning("%s", reason_msg)
            return
        services.ensure_vllm_anthropic_mode()

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
    # WOR-363: capture pool size BEFORE launching this worker. Counts OTHER
    # workers — does not include the one we're about to add.
    dispatch_concurrency = len(_local_active) + len(_cloud_active)

    # WOR-370: vLLM /metrics attribution gate.
    # 1. If we're solo (dispatch_concurrency==0) and the /metrics endpoint
    #    responds, snapshot for later delta computation.
    # 2. If we're NOT solo, any peer that previously had remained_solo=True
    #    must lose that flag — they're no longer alone, so their session's
    #    deltas would be polluted by this worker's traffic.
    vllm_metrics_before: dict[str, float] | None = None
    remained_solo = False
    if dispatch_concurrency == 0:
        snapshot = capture_vllm_metrics()
        if snapshot is not None:
            vllm_metrics_before = snapshot
            remained_solo = True
    else:
        for peer in (*_local_active, *_cloud_active):
            if peer.remained_solo:
                peer.remained_solo = False

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
        dispatch_concurrency=dispatch_concurrency,
        vllm_metrics_before=vllm_metrics_before,
        remained_solo=remained_solo,
    )
    if effective_mode == "local":
        _local_active.append(worker)
    else:
        _cloud_active.append(worker)
