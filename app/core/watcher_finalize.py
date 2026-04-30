"""Free functions implementing worker finalization logic.

Extracted from Watcher._finalize_worker to reduce watcher.py LOC toward the
≤500 Recommend tier and bring cognitive complexity below SonarCloud threshold.
"""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404
from pathlib import Path

from app.core.escalation_policy import EscalationPolicy, ImprovementLogConfig
from app.core.linear_client import LinearError
from app.core.manifest import ExecutionManifest
from app.core.metrics import MetricsStore, Outcome, ReworkEvent, TicketMetrics
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
    outcome, escalated, artifacts_preserved, sonar_findings, result_data = (
        _execute_finalization(
            worker, returncode, linear, escalation_policy, repo_root, metrics
        )
    )

    log_path = worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
    input_tokens, output_tokens, context_compactions = _parse_worker_usage(log_path)
    # Backward-compat: local_tokens = input + output (None when either is None)
    local_tokens: int | None = (
        (input_tokens or 0) + (output_tokens or 0)
        if input_tokens is not None and output_tokens is not None
        else None
    )
    # Derive throughput when both output tokens and wall time are available
    local_output_tokens_per_second: float | None = None
    if output_tokens is not None and wall_time and wall_time > 0:
        local_output_tokens_per_second = output_tokens / wall_time
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
            local_input_tokens=input_tokens,
            local_output_tokens=output_tokens,
            local_tokens=local_tokens,
            local_wall_time=wall_time,
            local_output_tokens_per_second=local_output_tokens_per_second,
            escalated_to_cloud=escalated,
            outcome=outcome,
            retry_count=worker.retry_count,
            context_compactions=context_compactions,
            sonar_findings_count=(
                len(sonar_findings) if sonar_findings is not None else None
            ),
        )
    )

    # Improvement log — append a one-line finding after each worker session
    # Skip when result_data is missing/empty (e.g. result.json not yet written).
    if escalation_policy.improvement_log is not None and result_data:
        write_improvement_log_finding(
            linear=linear,
            linear_id=worker.linear_id,
            improvement_log_config=escalation_policy.improvement_log,
            result_data=result_data,
            ticket_id=worker.ticket_id,
            epic_id=worker.manifest.epic_id or "",
            wall_time=wall_time,
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
    metrics: MetricsStore,
) -> tuple[Outcome, bool, bool, list[str] | None, dict[str, object] | None]:
    """Determine outcome, escalation status, and artifact state.

    Returns (outcome, escalated, artifacts_preserved, sonar_findings,
    result_data).  *result_data* is read from the worker's result.json so that
    callers can produce improvement-log comments without re-reading the file.
    """
    manifest = worker.manifest
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id
    result_path = repo_root / manifest.artifact_paths.result_json
    result_data = _read_result_data(result_path)

    if returncode != 0:
        logger.error("Worker %s exited non-zero (%d)", ticket_id, returncode)
        escalated = bool(manifest.failure_policy.escalate_to_cloud)
        if escalated:
            metrics.record_rework_event(
                ReworkEvent(
                    ticket_id=ticket_id,
                    model_id=worker.manifest.epic_id or "",
                    rework_reason="escalated",
                    rework_cost_minutes=0.0,
                )
            )
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
        return "failure", escalated, False, None, result_data

    checks_ok = run_checks(manifest, worker.worktree_path)
    if not checks_ok:
        worker.retry_count += 1
        metrics.record_rework_event(
            ReworkEvent(
                ticket_id=ticket_id,
                model_id=worker.manifest.epic_id or "",
                rework_reason="local_retry",
                rework_cost_minutes=0.0,
            )
        )
    if not checks_ok and manifest.failure_policy.on_check_failure == "abort":
        escalated = bool(manifest.failure_policy.escalate_to_cloud)
        if escalated:
            metrics.record_rework_event(
                ReworkEvent(
                    ticket_id=ticket_id,
                    model_id=worker.manifest.epic_id or "",
                    rework_reason="escalated",
                    rework_cost_minutes=0.0,
                )
            )
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
        return "failure", escalated, False, None, result_data

    preserve_worker_artifacts(repo_root, worker)
    flags = _read_result_flags(result_path)
    action = escalation_policy.classify_result(**flags)

    outcome, escalated, sonar_findings = _handle_policy_outcome(
        action, flags, worker, linear, escalation_policy, metrics
    )
    return outcome, escalated, True, sonar_findings, result_data


def _handle_policy_outcome(
    action: str,
    flags: dict[str, bool],
    worker: ActiveWorker,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
    metrics: MetricsStore,
) -> tuple[Outcome, bool, list[str] | None]:
    """Map a policy action to an outcome, posting Linear comments as needed."""
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id
    manifest = worker.manifest

    if action == "escalate":
        triggering = next((f for f in _POLICY_FLAGS if flags.get(f)), "unknown")
        logger.info("Escalating %s to cloud (flag=%s)", ticket_id, triggering)
        metrics.record_rework_event(
            ReworkEvent(
                ticket_id=ticket_id,
                model_id=worker.manifest.epic_id or "",
                rework_reason="escalated",
                rework_cost_minutes=0.0,
            )
        )
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
        metrics.record_rework_event(
            ReworkEvent(
                ticket_id=ticket_id,
                model_id=worker.manifest.epic_id or "",
                rework_reason="escalated",
                rework_cost_minutes=0.0,
            )
        )
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


# ---------------------------------------------------------------------------
# Improvement log
# ---------------------------------------------------------------------------

_CATEGORY_HIERARCHY: tuple[tuple[str, bool, bool], ...] = (
    ("scope", True, False),
    ("quality", False, True),
    ("perf", False, False),
    ("escalation", False, False),
    ("improvement", False, False),
)


def _infer_category(
    *,
    scope_drift: bool,
    forbidden_path_touched: bool,
    check_failures: list[str],
    wall_time: float,
    runtime_threshold_minutes: int,
    escalated: bool,
    notes: str,
    status: str,
) -> str:
    """Infer the improvement-log category from result data.

    Priority order:
      1. scope_drift / forbidden_path → scope
      2. check_failures → quality
      3. wall_time over threshold → perf
      4. escalated to cloud → escalation
      5. default → improvement
    """
    if scope_drift or forbidden_path_touched:
        return "scope"
    if check_failures:
        return "quality"
    if wall_time > runtime_threshold_minutes * 60 and wall_time > 0:
        return "perf"
    if escalated:
        return "escalation"
    if status == "success" and notes:
        return "improvement"
    return "improvement"


# fmt: off
_RESULT_DATA_KEYS = (
    "ticket_id", "epic_id", "status", "summary", "notes",
    "checks_failed", "scope_drift", "forbidden_path_touched",
    "escalated_to_cloud",
)
# fmt: on


def _read_result_data(result_path: Path) -> dict[str, object] | None:
    """Read the worker result.json and return selected fields.

    Returns a dict with the keys in `_RESULT_DATA_KEYS` when the file
    exists and is valid JSON.  Returns ``None`` when the file is missing
    or malformed — callers should handle the absence gracefully.
    """
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {k: raw.get(k) for k in _RESULT_DATA_KEYS}


def write_improvement_log_finding(
    *,
    linear: LinearClientProtocol,
    linear_id: str,
    improvement_log_config: ImprovementLogConfig,
    result_data: dict[str, object] | None,
    ticket_id: str,
    epic_id: str,
    wall_time: float,
) -> None:
    """Append a one-line finding to the improvement log ticket.

    If the comment count on the improvement log ticket exceeds
    ``improvement_log.review_threshold``, the ticket state is set to
    ``ReadyForReview`` and a warning is logged.

    Called after each worker finalization when the policy configures an
    improvement-log ticket.
    """
    if not improvement_log_config or result_data is None:
        return

    log_ticket_id = improvement_log_config.ticket_id
    review_threshold = improvement_log_config.review_threshold
    runtime_threshold = improvement_log_config.runtime_threshold_minutes

    # Extract fields from result data for category inference
    # Use `or` fallback because the value may be explicitly None in the JSON.
    r_scope = bool(result_data.get("scope_drift"))
    r_forbidden = bool(result_data.get("forbidden_path_touched"))
    cf_raw = result_data.get("checks_failed")
    cf_list = cf_raw if isinstance(cf_raw, list) else []
    r_check_failures = [str(item) for item in cf_list]
    r_escalated = bool(result_data.get("escalated_to_cloud"))
    r_notes = str(result_data.get("notes") or "")
    r_status = str(result_data.get("status") or "")
    r_ticket_id = str(result_data.get("ticket_id") or ticket_id)
    r_epic_id = str(result_data.get("epic_id") or epic_id or "")

    category = _infer_category(
        scope_drift=r_scope,
        forbidden_path_touched=r_forbidden,
        check_failures=r_check_failures,
        wall_time=wall_time,
        runtime_threshold_minutes=runtime_threshold,
        escalated=r_escalated,
        notes=r_notes,
        status=r_status,
    )

    # Build the one-sentence finding from available data
    finding_parts: list[str] = []
    if r_check_failures:
        failing = ", ".join(r_check_failures)
        finding_parts.append(f"{len(r_check_failures)} check(s) failed ({failing})")
    elif r_notes:
        finding_parts.append(r_notes)
    else:
        finding_parts.append("Worker session completed without issues")

    one_sentence = finding_parts[0] if finding_parts else "no details available"

    runtime_min = int(wall_time) // 60 if wall_time > 0 else 0

    comment_body = (
        f"[{r_ticket_id} / epic {r_epic_id} / {runtime_min}min] "
        f"{category}: {one_sentence}"
    )

    # Append the comment
    _try_post_comment(linear, log_ticket_id, r_ticket_id, comment_body)

    # Count existing comments and check threshold
    try:
        comments = linear.list_comments(log_ticket_id)
        comment_count = len(comments)
        if comment_count > review_threshold:
            logger.warning(
                "Improvement log ticket %s has %d findings "
                "(threshold %d) — setting to ReadyForReview",
                log_ticket_id,
                comment_count,
                review_threshold,
            )
            safe_set_state(linear, log_ticket_id, "ReadyForReview", log_ticket_id)
    except Exception:
        logger.warning(
            "Could not fetch comment count for improvement log ticket %s",
            log_ticket_id,
        )
