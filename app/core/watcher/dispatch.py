"""Dispatch functions for the watcher orchestrator.

Extracted from Watcher methods to reduce watcher.py LOC toward the
≤500 Recommend tier. Each function is a module-level callable that
receives all context it needs as explicit parameters — no class
instantiation required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.linear_client import DONE_STATE_TYPES
from app.core.manifest import ExecutionManifest

from .watcher_finalize import safe_set_state
from .watcher_helpers import (
    DEFAULT_KV_RESERVATION,
    EFFORT_KV_RESERVATION,
    capture_vllm_metrics_diagnostic,
    check_allowed_paths_overlap,
    count_main_ahead_of_epic,
    get_active_parent_ids,
    kv_admission_ok,
    resolve_effective_mode,
    suppress_dedup,
)
from .watcher_services import ServiceManager
from .watcher_subprocess import launch_worker
from .watcher_types import _VLLM_BASE_URL, ActiveWorker, LinearClientProtocol
from .watcher_worktrees import (
    backup_plan_files,
    cleanup_orphan_dir,
    cleanup_stale_artifacts,
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


@dataclass
class DispatchContext:
    """Watcher-cycle-level state shared across every ``start_ticket`` call
    in a poll cycle (WOR-510, S107).

    The per-ticket args (manifest, linear_id, ticket_id, candidate) stay
    direct on :func:`start_ticket`; everything that is constant across
    tickets within a cycle is bundled here. Field values are exactly the
    former positional params (the leading underscores were unused-arg
    markers — these are all genuinely used), so behaviour is identical.
    """

    linear: LinearClientProtocol
    services: ServiceManager
    worker_verbose: bool
    local_active: list[ActiveWorker]
    cloud_active: list[ActiveWorker]
    max_cloud_workers: int
    repo_root: Path
    processed_tickets: list[object]
    escalation_policy: object
    dedup_state: dict[str, str]
    kv_budget: int | None = None


def _run_preflight(
    manifest: ExecutionManifest,
    linear_id: str,
    ticket_id: str,
    ctx: DispatchContext,
) -> str | None:
    """All pre-dispatch gates + routing resolution (WOR-510, S3776).

    Returns the resolved ``effective_mode``, or ``None`` if any gate
    aborted dispatch (the caller then just returns). Behaviour identical
    to the former inline gate sequence — same gates, same order, same
    early-return semantics, same refused-routing handling.
    """
    if not _check_blocker_preconditions(ctx.linear, manifest, linear_id, ticket_id):
        return None
    if not _check_epic_branch_overlap(
        ctx.linear, manifest, ctx.local_active, linear_id, ticket_id
    ):
        return None
    if not _check_manifest_quality(ctx.linear, manifest, linear_id, ticket_id):
        return None
    if not _check_epic_drift(ctx.linear, manifest, ctx.repo_root, linear_id, ticket_id):
        return None
    if not _check_in_progress_local(ctx.linear, linear_id, ticket_id):
        return None
    if not _check_path_overlap(
        manifest, ctx.local_active, ctx.cloud_active, ticket_id, ctx.dedup_state
    ):
        return None
    if not _check_kv_budget(
        manifest, ctx.local_active, ticket_id, ctx.dedup_state, ctx.kv_budget
    ):
        return None

    effective_mode = resolve_effective_mode(
        ctx.services._mode if hasattr(ctx.services, "_mode") else "local",
        manifest.routing,
    )
    if effective_mode == "refused":
        logger.warning(
            "Skipping %s — manifest declares routing=cloud_only "
            "but the watcher is in local-only mode",
            ticket_id,
        )
        ctx.linear.post_comment(
            linear_id,
            (
                "Skipping dispatch — the manifest declares "
                "routing=cloud_only but the watcher daemon is "
                "running in local-only mode (--worker-mode local). "
                "Start the daemon with --worker-mode cloud or "
                "--worker-mode default to dispatch this ticket."
            ),
        )
        return None

    if not _check_pool_capacity(
        effective_mode, ctx.cloud_active, ctx.max_cloud_workers, ticket_id
    ):
        return None
    if not _check_vllm_health(effective_mode, ctx.services, ticket_id, ctx.dedup_state):
        return None
    return effective_mode


def start_ticket(
    manifest: ExecutionManifest,
    linear_id: str,
    ticket_id: str,
    ctx: DispatchContext,
    *,
    candidate: dict[str, Any] | None = None,
) -> None:
    """Run the full ticket-start flow (WOR-431: prereqs + dispatch).

    *candidate* is the raw Linear ticket dict with parent info (WOR-220)
    used to compute ``same_epic_pair`` at dispatch time. WOR-510: the 11
    watcher-cycle params are bundled into *ctx* (S107) and the preflight
    gate cluster extracted to :func:`_run_preflight` (S3776) — behaviour
    identical.
    """
    effective_mode = _run_preflight(manifest, linear_id, ticket_id, ctx)
    if effective_mode is None:
        return

    # WOR-458: pre-dispatch cleanup — remove stale artifacts and orphan dirs
    # that would otherwise block the new worktree creation.
    _cleanup_stale_state(ctx.repo_root, manifest, ticket_id)

    worktree_path = create_worktree(ctx.repo_root, manifest)
    copy_manifest_to_worktree(ctx.repo_root, manifest, worktree_path)
    write_worker_pytest_config(worktree_path)

    safe_set_state(
        ctx.linear,
        linear_id,
        manifest.ticket_state_map.in_progress_local,
        ticket_id,
    )
    logger.info("Launching worker for %s (mode=%s)", ticket_id, effective_mode)

    backed_up_plans = backup_plan_files()
    # WOR-363: capture pool size BEFORE launching this worker. Counts OTHER
    # workers — does not include the one we're about to add.
    dispatch_concurrency = len(ctx.local_active) + len(ctx.cloud_active)
    vllm_metrics_before, remained_solo = _snapshot_vllm_solo(
        dispatch_concurrency, ctx.local_active, ctx.cloud_active
    )

    # WOR-220: compute same_epic_pair — True when this candidate's parent
    # matches any active worker's parent (epic_id).
    same_epic_pair = False
    if candidate is not None:
        parent_id = (candidate.get("parent") or {}).get("id") or ""
        active_parents = get_active_parent_ids(ctx.local_active + ctx.cloud_active)
        same_epic_pair = bool(parent_id and parent_id in active_parents)

    process = launch_worker(
        ctx.repo_root, manifest, worktree_path, effective_mode, ctx.worker_verbose
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
        same_epic_pair=same_epic_pair,
    )
    if effective_mode == "local":
        ctx.local_active.append(worker)
    else:
        ctx.cloud_active.append(worker)


def _check_blocker_preconditions(
    linear: LinearClientProtocol,
    manifest: ExecutionManifest,
    linear_id: str,
    ticket_id: str,
) -> bool:
    """Return True to proceed; False to defer silently.

    Skips dispatch when Linear lists open blockers OR when
    manifest.blocked_by_tickets declares dependencies that have not reached a
    Done-equivalent state yet.
    """
    open_blockers = linear.get_open_blockers(linear_id)
    if open_blockers:
        logger.info("Skipping %s - open blockers: %s", ticket_id, open_blockers)
        return False
    for blocker_id in manifest.blocked_by_tickets:
        state_type = linear.get_issue_state_type(blocker_id)
        if state_type not in DONE_STATE_TYPES:
            logger.info(
                "Skipping %s - manifest declares unmerged blocker %s (state=%s)",
                ticket_id,
                blocker_id,
                state_type,
            )
            return False
    return True


def _check_epic_branch_overlap(
    linear: LinearClientProtocol,
    manifest: ExecutionManifest,
    _local_active: list[ActiveWorker],
    linear_id: str,
    ticket_id: str,
) -> bool:
    """WOR-419: defense-in-depth.

    Refuse to spawn a worker on a new epic/* branch when another epic/*
    branch is already in flight. Sub-ticket branches under the same epic
    are unaffected.
    """
    if not manifest.base_branch.startswith("epic/"):
        return True
    for worker in _local_active:
        if not hasattr(worker, "manifest"):
            continue
        peer_base = worker.manifest.base_branch
        if not peer_base.startswith("epic/") or peer_base == manifest.base_branch:
            continue
        logger.warning(
            "Deferring %s — epic branch %s already in-flight (worker on %s)",
            ticket_id,
            manifest.base_branch,
            peer_base,
        )
        linear.post_comment(
            linear_id,
            (
                f"Dispatch deferred: another worker is already "
                f"in-flight on epic branch `{peer_base}`. "
                f"Cannot dispatch to a new epic branch "
                f"`{manifest.base_branch}` until the in-flight "
                f"worker completes (one-active-epic-branch "
                f"principle, WOR-419)."
            ),
        )
        return False
    return True


def _check_manifest_quality(
    linear: LinearClientProtocol,
    manifest: ExecutionManifest,
    linear_id: str,
    ticket_id: str,
) -> bool:
    """WOR-378 quality gates: empty allowed_paths for local routing or empty
    required_checks -> reject."""
    if manifest.routing == "local" and not manifest.allowed_paths:
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
        return False
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
        return False
    return True


def _check_epic_drift(
    linear: LinearClientProtocol,
    manifest: ExecutionManifest,
    _repo_root: Path,
    linear_id: str,
    ticket_id: str,
) -> bool:
    """WOR-373: refuse stale epics.

    Stale epics silently drop main-side work during merge conflict
    resolution (WOR-282 forensic: 13 tests lost on a 107-commit-behind epic).
    """
    drift = count_main_ahead_of_epic(manifest.base_branch, _repo_root)
    if drift <= _EPIC_DRIFT_THRESHOLD:
        return True
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
    return False


def _check_path_overlap(
    manifest: ExecutionManifest,
    _local_active: list[ActiveWorker],
    _cloud_active: list[ActiveWorker],
    ticket_id: str,
    _dedup_state: dict[str, str],
) -> bool:
    """WOR-431: defer when allowed_paths overlap with any active worker."""
    conflicts = check_allowed_paths_overlap(_local_active + _cloud_active, manifest)
    if not conflicts:
        return True
    reason = f"overlap:{','.join(conflicts)}"
    reason_msg = "Deferring %s - allowed_paths overlap with active workers: %s" % (
        ticket_id,
        conflicts,
    )
    if suppress_dedup(ticket_id, reason, reason_msg, _dedup_state):
        logger.info(reason_msg)
    return False


# WOR-525: a freshly-restarted daemon has no in-process memory of which
# tickets it already handled, so a stale ``ReadyForLocal`` manifest in
# .claude/artifacts/ for an already-completed ticket gets re-dispatched
# (observed live: WOR-515/516/517 re-run after a restart, all already merged
# via the WOR-519 epic). These workflow-state names are terminal in the
# repo-scaffold-desktop Linear workflow; a ticket in any of them must never
# be (re-)dispatched regardless of its manifest status.
_TERMINAL_STATE_NAMES = frozenset(
    {"Done", "MergedToEpic", "Cancelled", "Canceled", "Duplicate"}
)


def _check_in_progress_local(
    linear: LinearClientProtocol,
    linear_id: str,
    ticket_id: str,
) -> bool:
    """WOR-458 + WOR-525: refuse dispatch when the ticket's Linear state
    means it must not be (re-)run.

    Refuses ``InProgressLocal`` (WOR-458 double-launch guard) and any
    terminal state — ``Done`` / ``MergedToEpic`` / ``Cancelled`` /
    ``Duplicate`` (WOR-525). The Linear state lock normally keeps the
    watcher from picking these up, but a freshly-restarted daemon would
    otherwise re-dispatch a completed ticket from a stale ReadyForLocal
    manifest in .claude/artifacts/ — this reconciles against Linear's
    authoritative state before any worktree is created.
    """
    state_name = linear.get_current_state_name(linear_id)
    if state_name == "InProgressLocal":
        logger.warning(
            "Deferring %s — ticket is already InProgressLocal (state=%s). "
            "Double-launch guard.",
            ticket_id,
            state_name,
        )
        return False
    if state_name in _TERMINAL_STATE_NAMES:
        logger.warning(
            "Skipping %s — Linear state is terminal (state=%s); not "
            "re-dispatching a completed ticket from a stale ReadyForLocal "
            "manifest (WOR-525).",
            ticket_id,
            state_name,
        )
        return False
    return True


def _check_pool_capacity(
    effective_mode: str,
    _cloud_active: list[ActiveWorker],
    max_cloud_workers: int,
    ticket_id: str,
) -> bool:
    """Defer cloud dispatch when the cloud pool is at capacity."""
    if effective_mode == "local" or len(_cloud_active) < max_cloud_workers:
        return True
    logger.info(
        "Deferring %s — cloud pool full (%d/%d)",
        ticket_id,
        len(_cloud_active),
        max_cloud_workers,
    )
    return False


def _check_vllm_health(
    effective_mode: str,
    services: ServiceManager,
    ticket_id: str,
    _dedup_state: dict[str, str],
) -> bool:
    """For local mode: defer until vLLM probes healthy + Anthropic mode is set."""
    if effective_mode != "local":
        return True
    if not services.probe_vllm_health():
        reason_msg = "Deferring %s — vLLM not ready yet" % (ticket_id,)
        if suppress_dedup(ticket_id, "vllm_not_ready", reason_msg, _dedup_state):
            logger.warning("%s", reason_msg)
        return False
    services.ensure_vllm_anthropic_mode()
    return True


def _check_kv_budget(
    manifest: ExecutionManifest,
    _local_active: list[ActiveWorker],
    ticket_id: str,
    _dedup_state: dict[str, str],
    _kv_budget: int | None = None,
) -> bool:
    """WOR-502: effort-aware KV-budget admission for local workers.

    Only applies when ``effective_mode`` will be ``"local"`` — checked via
    ``manifest.routing`` (cloud_preferred / cloud_only route to cloud and
    skip this gate).  Sums KV reservations of in-flight local workers plus
    the candidate's own reservation and admits only when within budget.

    Returns ``True`` to proceed; ``False`` to defer.
    """
    # Skip for cloud-bound tickets (local routing is the only path to
    # local workers; cloud_preferred/cloud_only route to the cloud).
    if manifest.routing not in ("local",):
        return True

    # Budget is optional — when None the gate is effectively infinite.
    budget = _kv_budget if _kv_budget is not None else 2**31

    in_flight_efforts = [w.manifest.effort for w in _local_active]
    if not kv_admission_ok(in_flight_efforts, manifest.effort, budget=budget):

        def _kv_for(e: str | None) -> int:
            return EFFORT_KV_RESERVATION.get(e, DEFAULT_KV_RESERVATION)  # type: ignore[arg-type]

        in_flight_kv = sum(_kv_for(e) for e in in_flight_efforts)
        candidate_kv = _kv_for(manifest.effort)
        reason_msg = (
            "Deferring %s — KV budget exhausted "
            "(in-flight=%d, candidate=%d, budget=%d)"
            % (ticket_id, in_flight_kv, candidate_kv, budget)
        )
        if suppress_dedup(ticket_id, "kv_budget_full", reason_msg, _dedup_state):
            logger.info(reason_msg)
        return False
    return True


def _cleanup_stale_state(
    repo_root: Path,
    manifest: ExecutionManifest,
    ticket_id: str,
) -> None:
    """WOR-458: remove stale artifacts and orphan directories before dispatch.

    Cleans up:
    - Stale ``result.json`` and ``worker_*.log`` in the artifact dir.
    - Orphan worktree directory at the expected path (not tracked by git).
    """
    from .watcher_types import _WORKTREE_BASE

    artifact_dir = (repo_root / manifest.artifact_paths.result_json).parent
    cleaned = cleanup_stale_artifacts(artifact_dir, ticket_id)
    if cleaned:
        logger.info("Cleaned %d stale artifact(s) for %s", len(cleaned), ticket_id)

    # Remove orphan worktree directory if present
    worktree_name = manifest.worktree_name or manifest.worker_branch
    worktree_path = repo_root.parent / _WORKTREE_BASE / worktree_name
    cleanup_orphan_dir(worktree_path)


def _snapshot_vllm_solo(
    dispatch_concurrency: int,
    _local_active: list[ActiveWorker],
    _cloud_active: list[ActiveWorker],
) -> tuple[dict[str, float] | None, bool]:
    """WOR-370: vLLM /metrics attribution gate.

    1. Solo (dispatch_concurrency==0): snapshot /metrics for later delta;
       returns (snapshot, True) on success; on probe failure logs a
       WARNING with the diagnostic reason and returns (None, False)
       (WOR-439 — the failure used to be silent at debug level).
    2. Not solo: clear remained_solo on any peer that previously had it set
       — their deltas would now be polluted by this worker's traffic.
       Returns (None, False).
    """
    if dispatch_concurrency == 0:
        snapshot, reason = capture_vllm_metrics_diagnostic()
        if snapshot is not None:
            return snapshot, True
        logger.warning(
            "vLLM /metrics solo snapshot failed (reason=%s, base_url=%s) — "
            "vllm_* columns will be NULL for this dispatch. If "
            "'unreachable', start vLLM or set WATCHER_VLLM_BASE_URL; if "
            "'no_counters_matched', this vLLM build renamed its Prometheus "
            "counters and _VLLM_COUNTERS / _VLLM_COUNTER_ALIASES need a "
            "verified update (WOR-439).",
            reason,
            _VLLM_BASE_URL,
        )
        return None, False
    for peer in (*_local_active, *_cloud_active):
        if peer.remained_solo:
            peer.remained_solo = False
    return None, False
