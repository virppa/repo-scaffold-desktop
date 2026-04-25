"""Free functions implementing worker finalization logic.

Extracted from Watcher._finalize_worker to reduce watcher.py LOC toward the
≤500 Recommend tier and bring cognitive complexity below SonarCloud threshold.
"""

from __future__ import annotations

import logging
import subprocess  # nosec B404
from pathlib import Path

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import LinearError
from app.core.manifest import ExecutionManifest
from app.core.metrics import MetricsStore, Outcome, TicketMetrics
from app.core.watcher_helpers import (
    _POLICY_FLAGS,
    _parse_worker_usage,
    _read_result_flags,
    resolve_effective_mode,
)
from app.core.watcher_subprocess import (
    create_pr,
    fetch_sonar_findings,
    run_checks,
)
from app.core.watcher_types import (
    _LOCAL_MODEL,
    ActiveWorker,
    LinearClientProtocol,
    _to_metrics_mode,
)
from app.core.watcher_worktrees import (
    cleanup_worktree,
    preserve_worker_artifacts,
    restore_plan_files,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def safe_set_state(
    linear: LinearClientProtocol,
    linear_id: str,
    state: str,
    ticket_id: str,
) -> None:
    try:
        linear.set_state(linear_id, state)
    except LinearError as exc:
        logger.warning("set_state failed for %s (state=%s): %s", ticket_id, state, exc)


def attempt_pr(
    manifest: ExecutionManifest,
    worker: ActiveWorker,
    linear: LinearClientProtocol,
) -> Outcome:
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id
    try:
        pr_url = create_pr(manifest, worker.worktree_path)
    except subprocess.CalledProcessError as exc:
        err_detail = (exc.stderr or exc.stdout or str(exc)).strip()
        logger.error("PR creation failed for %s: %s", ticket_id, err_detail)
        safe_set_state(linear, linear_id, manifest.ticket_state_map.failed, ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"PR creation failed for `{ticket_id}`:\n```\n{err_detail}\n```",
        )
        return "failure"
    logger.info("PR created for %s: %s", ticket_id, pr_url)
    return "success"


def finalize_worker(
    worker: ActiveWorker,
    *,
    returncode: int,
    wall_time: float,
    linear: LinearClientProtocol,
    metrics: MetricsStore,
    escalation_policy: EscalationPolicy,
    repo_root: Path,
    mode: str,
    project_id: str,
) -> None:
    outcome, escalated, artifacts_preserved, sonar_findings = _execute_finalization(
        worker, returncode, linear, escalation_policy, repo_root
    )

    log_path = worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
    local_tokens, context_compactions = _parse_worker_usage(log_path)
    eff = resolve_effective_mode(mode, worker.manifest.implementation_mode)
    metrics.record(
        TicketMetrics(
            ticket_id=worker.ticket_id,
            project_id=project_id,
            epic_id=worker.manifest.epic_id,
            implementation_mode=_to_metrics_mode(eff),
            local_used=(eff == "local"),
            local_model=(_LOCAL_MODEL if eff == "local" else None),
            cloud_used=(eff == "cloud"),
            local_tokens=local_tokens,
            local_wall_time=wall_time,
            escalated_to_cloud=escalated,
            outcome=outcome,
            retry_count=worker.retry_count,
            context_compactions=context_compactions,
            sonar_findings_count=(
                len(sonar_findings) if sonar_findings is not None else None
            ),
        )
    )

    restore_plan_files(worker.backed_up_plans)
    if not artifacts_preserved:
        preserve_worker_artifacts(repo_root, worker)
    cleanup_worktree(repo_root, worker.worktree_path)


# ---------------------------------------------------------------------------
# Internal helpers (reduce cognitive complexity of finalize_worker)
# ---------------------------------------------------------------------------


def _execute_finalization(
    worker: ActiveWorker,
    returncode: int,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
    repo_root: Path,
) -> tuple[Outcome, bool, bool, list[str] | None]:
    """Determine outcome, escalation status, and artifact state.

    Returns (outcome, escalated, artifacts_preserved, sonar_findings).
    """
    manifest = worker.manifest
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id

    if returncode != 0:
        logger.error("Worker %s exited non-zero (%d)", ticket_id, returncode)
        escalated = bool(manifest.failure_policy.escalate_to_cloud)
        if escalated:
            logger.info("Escalating %s to cloud per failure policy", ticket_id)
            safe_set_state(linear, linear_id, "In Progress", ticket_id)
            _try_post_comment(
                linear,
                linear_id,
                ticket_id,
                f"Local worker failed for `{ticket_id}` (non-zero exit). "
                f"Escalating to cloud per failure policy.",
            )
        else:
            safe_set_state(
                linear, linear_id, manifest.ticket_state_map.failed, ticket_id
            )
        return "failure", escalated, False, None

    checks_ok = run_checks(manifest, worker.worktree_path)
    if not checks_ok:
        worker.retry_count += 1
    if not checks_ok and manifest.failure_policy.on_check_failure == "abort":
        escalated = bool(manifest.failure_policy.escalate_to_cloud)
        if escalated:
            logger.info("Escalating %s to cloud after check failure", ticket_id)
            safe_set_state(linear, linear_id, "In Progress", ticket_id)
            _try_post_comment(
                linear,
                linear_id,
                ticket_id,
                f"Local worker failed checks for `{ticket_id}`. "
                f"Escalating to cloud per failure policy.",
            )
        else:
            safe_set_state(
                linear, linear_id, manifest.ticket_state_map.failed, ticket_id
            )
        return "failure", escalated, False, None

    preserve_worker_artifacts(repo_root, worker)
    flags = _read_result_flags(repo_root / manifest.artifact_paths.result_json)
    action = escalation_policy.classify_result(**flags)

    outcome, escalated, sonar_findings = _handle_policy_outcome(
        action, flags, worker, linear, escalation_policy
    )
    return outcome, escalated, True, sonar_findings


def _handle_policy_outcome(
    action: str,
    flags: dict[str, bool],
    worker: ActiveWorker,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
) -> tuple[Outcome, bool, list[str] | None]:
    """Map a policy action to an outcome, posting Linear comments as needed."""
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id
    manifest = worker.manifest

    if action == "escalate":
        triggering = next((f for f in _POLICY_FLAGS if flags.get(f)), "unknown")
        logger.info("Escalating %s to cloud (flag=%s)", ticket_id, triggering)
        safe_set_state(linear, linear_id, "In Progress", ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"Local worker escalating `{ticket_id}` to cloud. "
            f"Triggering flag: `{triggering}`.",
        )
        return "escalated", True, None

    if action == "human":
        logger.info("Human review required for %s per policy", ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"Human review required for `{ticket_id}` before "
            f"proceeding. Please inspect the result artifact.",
        )
        return "aborted", False, None

    # fix_locally — check Sonar findings before creating PR
    sonar_findings = fetch_sonar_findings(manifest.worker_branch)
    if _sonar_requires_escalation(
        sonar_findings, ticket_id, linear_id, linear, escalation_policy
    ):
        safe_set_state(linear, linear_id, "In Progress", ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"Local worker escalating `{ticket_id}` to cloud due "
            f"to Sonar finding requiring immediate action.",
        )
        return "escalated", True, sonar_findings

    outcome = attempt_pr(manifest, worker, linear)
    return outcome, False, sonar_findings


def _sonar_requires_escalation(
    sonar_findings: list[str] | None,
    ticket_id: str,
    linear_id: str,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
) -> bool:
    if not sonar_findings:
        return False
    for severity in sonar_findings:
        action = escalation_policy.classify_sonar_finding(severity.lower())
        if action == "escalate":
            return True
        logger.warning(
            "Sonar finding for %s: severity=%s — fix_locally", ticket_id, severity
        )
    return False


def _try_post_comment(
    linear: LinearClientProtocol,
    linear_id: str,
    ticket_id: str,
    body: str,
) -> None:
    try:
        linear.post_comment(linear_id, body)
    except Exception:
        logger.warning("Could not post comment for %s", ticket_id)
