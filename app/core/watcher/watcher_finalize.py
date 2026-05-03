"""Free functions implementing worker finalization logic.

Extracted from Watcher._finalize_worker to reduce watcher.py LOC toward the
≤500 Recommend tier and bring cognitive complexity below SonarCloud threshold.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess  # nosec B404
from pathlib import Path

from app.core.escalation_policy import EscalationPolicy, get_waste_warn_threshold
from app.core.linear_client import LinearError
from app.core.manifest import ExecutionManifest
from app.core.metrics import (
    MetricsStore,
    Outcome,
    TicketMetrics,
    TicketRunLog,
    compute_tags,
)

from .watcher_helpers import (
    _POLICY_FLAGS,
    _parse_worker_api_retries,
    _parse_worker_subagent_spawns,
    _parse_worker_usage,
    _read_result_flags,
    resolve_effective_mode,
)
from .watcher_subprocess import (
    create_pr,
    fetch_sonar_findings,
    parse_git_shortstat,
    run_checks,
)
from .watcher_tui import TrackedPR
from .watcher_types import (
    _CLAUDE_DIR,
    _IN_PROGRESS_STATE,
    _LOCAL_MODEL,
    ActiveWorker,
    LinearClientProtocol,
    _to_metrics_mode,
)
from .watcher_worktrees import (
    cleanup_worktree,
    commit_wip_state,
    preserve_worker_artifacts,
    restore_plan_files,
    squash_wip_commits,
)
from .worker_waste import compute_waste_score

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cloud pricing — per million tokens, keyed on model name
# ---------------------------------------------------------------------------

_CLOUD_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
}

_DEFAULT_CLOUD_MODEL = "claude-opus-4-7"


def _resolve_cloud_model() -> str:
    """Return the cloud model name.

    Checks ANTHROPIC_MODEL env-var first, falls back to ``_DEFAULT_CLOUD_MODEL``.
    """
    return os.environ.get("ANTHROPIC_MODEL") or _DEFAULT_CLOUD_MODEL


def _estimate_cloud_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    model: str,
) -> float:
    """Estimate the cloud API cost for the given token counts.

    Input tokens are billed at the model's input rate, output tokens at the
    output rate.  Prices are per million tokens.

    Returns 0.0 when the model is not found in the pricing table or either
    token count is None.
    """
    if input_tokens is None or output_tokens is None:
        return 0.0
    pricing = _CLOUD_PRICING.get(model)
    if pricing is None:
        return 0.0
    return (input_tokens / 1_000_000) * pricing["input"] + (
        output_tokens / 1_000_000
    ) * pricing["output"]


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
    tracked_prs: list[TrackedPR] | None = None,
) -> tuple[Outcome, str | None]:
    """Attempt PR creation.  Returns (outcome, pr_url).

    When *tracked_prs* is provided the PR is registered there on success.
    """
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id
    try:
        pr_url = create_pr(manifest, worker.worktree_path)
    except subprocess.CalledProcessError as exc:
        err_detail = (exc.stderr or exc.stdout or str(exc)).strip()
        logger.error("PR creation failed for %s: %s", ticket_id, err_detail)
        # Preserve any uncommitted worktree changes so a retry worker can
        # resume from them (WOR-267, WOR-288). attempt_pr does not own the
        # worktree teardown decision — finalize_worker handles that based on
        # the returned WipPreservationResult — but we still call commit_wip_state
        # here so the WIP commit happens before checks/PR phase artifacts diverge.
        commit_wip_state(
            worker.worktree_path,
            ticket_id,
            manifest.worker_branch,
        )
        safe_set_state(linear, linear_id, manifest.ticket_state_map.failed, ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"PR creation failed for `{ticket_id}`:\n```\n{err_detail}\n```",
        )
        return "failure", None
    logger.info("PR created for %s: %s", ticket_id, pr_url)

    # Register PR for auto-merge tracking (Phase 1).
    if tracked_prs is not None:
        try:
            # gh pr create outputs a URL like:
            # https://github.com/owner/repo/pull/123
            parts = pr_url.rstrip("/").split("/")
            if len(parts) >= 5:
                pr_number = int(parts[-1])
                tracked_prs.append(
                    TrackedPR(
                        number=pr_number,
                        base=manifest.base_branch,
                    )
                )
        except (IndexError, ValueError):
            logger.warning("Could not parse PR URL %s for tracking", pr_url)

    return "success", pr_url


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
    tracked_prs: list[TrackedPR] | None = None,
) -> Outcome:
    outcome, escalated, artifacts_preserved, sonar_findings, failed_checks, _ = (
        _execute_finalization(
            worker,
            returncode,
            linear,
            escalation_policy,
            repo_root,
            tracked_prs=tracked_prs,
            metrics=metrics,
            project_id=project_id,
        )
    )

    log_path = worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
    input_tokens, output_tokens, context_compactions, compact_duration_ms = (
        _parse_worker_usage(log_path)
    )
    api_retry_count = _parse_worker_api_retries(log_path)
    subagent_spawns = _parse_worker_subagent_spawns(log_path)
    eff = resolve_effective_mode(mode, worker.manifest.implementation_mode)

    # Parse git diff --shortstat to populate lines_changed / files_changed.
    # Three-dot diff against the merge-base — invariant under base_branch
    # advancing during the worker's lifetime (WOR-354). Two-dot would attribute
    # sibling-merge upstream commits to this worker.
    # Must run before preserve_worker_artifacts tears down the worktree.
    try:
        diff_output = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worker.worktree_path),
                "diff",
                "--shortstat",
                f"{worker.manifest.base_branch}...HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw_shortstat = (diff_output.stdout or "") + (diff_output.stderr or "")
        lines_changed, files_changed = parse_git_shortstat(raw_shortstat)
    except (OSError, subprocess.TimeoutExpired):
        lines_changed, files_changed = 0, 0

    # Cloud metrics — populated only when eff == "cloud"
    cloud_model: str | None = None
    cloud_tokens: int | None = None
    cloud_cost_estimate: float | None = None
    if eff == "cloud" and input_tokens is not None and output_tokens is not None:
        cloud_model = _resolve_cloud_model()
        cloud_tokens = input_tokens + output_tokens
        cloud_cost_estimate = _estimate_cloud_cost(
            input_tokens, output_tokens, cloud_model
        )

    # Local metrics — populated only when eff == "local"
    local_tokens: int | None = None
    local_input_tokens: int | None = None
    local_output_tokens: int | None = None
    output_tokens_per_wall_second: float | None = None
    local_model_str: str | None = None
    if eff == "local":
        local_tokens = (
            (input_tokens or 0) + (output_tokens or 0)
            if input_tokens is not None and output_tokens is not None
            else None
        )
        local_input_tokens = input_tokens
        local_output_tokens = output_tokens
        local_model_str = _LOCAL_MODEL
        if output_tokens is not None and wall_time and wall_time > 0:
            output_tokens_per_wall_second = output_tokens / wall_time

    # Compute waste score from the worker log (WOR-277).
    waste_report = compute_waste_score(log_path)
    waste_score: int | None = waste_report.score if waste_report.score > 0 else None
    waste_breakdown_json: str | None = (
        json.dumps(waste_report.breakdown) if waste_report.score > 0 else None
    )
    if waste_score is not None and waste_score > get_waste_warn_threshold():
        top_reasons = ", ".join(
            f"{k}={v}"
            for k, v in sorted(
                waste_report.breakdown.items(), key=lambda x: x[1], reverse=True
            )
            if v > 0
        )
        logger.warning(
            "High waste score for %s: %d/100 (%s)",
            worker.ticket_id,
            waste_score,
            top_reasons,
        )

    # Derive the first failed check name for TicketRunLog (WOR-261)
    first_failed_check: str | None = (
        str(failed_checks[0]["check"]) if failed_checks else None
    )
    # check_failures: store the list when non-empty, else None
    check_failures = failed_checks if failed_checks else None

    metrics.record(
        TicketMetrics(
            ticket_id=worker.ticket_id,
            project_id=project_id,
            epic_id=worker.manifest.epic_id,
            implementation_mode=_to_metrics_mode(eff),
            local_used=(eff == "local"),
            local_model=local_model_str,
            cloud_used=(eff == "cloud"),
            cloud_model=cloud_model,
            cloud_tokens=cloud_tokens,
            cloud_cost_estimate=cloud_cost_estimate,
            local_input_tokens=local_input_tokens,
            local_output_tokens=local_output_tokens,
            local_tokens=local_tokens,
            local_wall_time=wall_time,
            output_tokens_per_wall_second=output_tokens_per_wall_second,
            escalated_to_cloud=escalated,
            outcome=outcome,
            retry_count=worker.retry_count,
            context_compactions=context_compactions,
            check_failures=check_failures,
            lines_changed=lines_changed,
            files_changed=files_changed,
            sonar_findings_count=(
                len(sonar_findings) if sonar_findings is not None else None
            ),
            # WOR-262: copy taxonomy fields from manifest into metrics
            change_type=worker.manifest.change_type,
            reasoning_demand=worker.manifest.reasoning_demand,
            scope_clarity=worker.manifest.scope_clarity,
            constraint_density=worker.manifest.constraint_density,
            ac_specificity=worker.manifest.ac_specificity,
            tech_stack=worker.manifest.tech_stack,
            raw_extensions=worker.manifest.raw_extensions,
            waste_score=waste_score,
            waste_breakdown_json=waste_breakdown_json,
            # WOR-348: persist manifest effort for retro analytics
            effort=worker.manifest.effort,
            # WOR-358: persist total compaction time for throughput analysis
            compact_duration_ms=compact_duration_ms,
            # WOR-360: persist Claude Code's internal retry count
            api_retry_count=api_retry_count,
            # WOR-364: persist Task-tool subagent count
            subagent_spawns=subagent_spawns,
        )
    )
    metrics.record_run(
        TicketRunLog(
            ticket_id=worker.ticket_id,
            attempt=worker.retry_count + 1,
            implementation_mode=_to_metrics_mode(eff),
            outcome=outcome,
            failed_check=first_failed_check,
            wall_time_s=wall_time,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_tok_per_s=output_tokens_per_wall_second,
            context_compactions=context_compactions,
        )
    )

    # Auto-detect tags for morning retros (WOR-332).
    _manifest = worker.manifest
    _row = metrics.get_by_ticket(worker.ticket_id, project_id)
    if _row is not None:
        _tags = compute_tags(
            _row,
            _read_result_status(repo_root, _manifest) or "",
            _read_result_flags(repo_root / _manifest.artifact_paths.result_json),
            tracked_prs,
        )
        if _tags:
            metrics.set_tags(worker.ticket_id, project_id, _tags)

    restore_plan_files(worker.backed_up_plans)

    # Always preserve any uncommitted WIP before deciding cleanup (WOR-347).
    # Workers can exit rc=0 with all checks passing yet never have committed
    # the work — the previous success path skipped commit_wip_state and
    # destroyed uncommitted edits with the worktree. Running it on every path
    # makes the worst case "an extra WIP commit on the worker branch" rather
    # than "all work lost".
    backup_root = repo_root / _CLAUDE_DIR / "artifacts"
    wip_result = commit_wip_state(
        worker.worktree_path,
        worker.ticket_id,
        worker.manifest.worker_branch,
        backup_root=backup_root,
    )
    if wip_result.sha is not None:
        _write_wip_sha_to_last_failure(worker, wip_result.sha)

    if not artifacts_preserved:
        # Failure path: also preserve full worker artifacts (logs, last_failure,
        # etc.) for forensic analysis. Success path doesn't need this — the PR
        # itself is the artifact.
        preserve_worker_artifacts(repo_root, worker)

    if wip_result.status in ("clean", "pushed", "backup"):
        cleanup_worktree(repo_root, worker.worktree_path)
    else:
        # WOR-288: WIP preservation failed (commit_wip_state could not push
        # AND could not back up the dirty tree). Removing the worktree now
        # would destroy uncommitted work — leave it in place for human
        # salvage. The worktree path appears in the ERROR log so the
        # operator can git status / commit / push manually.
        logger.error(
            "WIP preservation failed for %s — leaving worktree in place at %s "
            "for manual recovery (error: %s). Run `git -C <path> status` to "
            "inspect, then commit + push to %s manually.",
            worker.ticket_id,
            worker.worktree_path,
            wip_result.error or "unknown",
            worker.manifest.worker_branch,
        )
    return outcome


# ---------------------------------------------------------------------------
# Internal helpers (reduce cognitive complexity of finalize_worker)
# ---------------------------------------------------------------------------


def _read_result_status(repo_root: Path, manifest: ExecutionManifest) -> str | None:
    """Return the worker's self-reported status from result.json, or None.

    Workers write a ``status`` field of ``"success"`` or ``"failure"`` (or
    similar) to ``result.json`` immediately before exiting. This is the
    in-band signal of how the work actually went, distinct from the OS-level
    subprocess exit code (which can be non-zero even after a successful run
    due to Claude Code CLI teardown quirks — see WOR-286).

    Returns None when the file is missing or unreadable; callers should treat
    that as "no in-band signal" rather than success.
    """
    result_path = repo_root / manifest.artifact_paths.result_json
    try:
        raw = result_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    status = data.get("status") if isinstance(data, dict) else None
    return status if isinstance(status, str) else None


def _execute_finalization(
    worker: ActiveWorker,
    returncode: int,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
    repo_root: Path,
    tracked_prs: list[TrackedPR] | None = None,
    metrics: MetricsStore | None = None,
    project_id: str = "",
) -> tuple[
    Outcome, bool, bool, list[str] | None, list[dict[str, int | str]], str | None
]:
    """Determine outcome, escalation status, and artifact state.

    Returns (outcome, escalated, artifacts_preserved, sonar_findings,
             failed_checks, pr_url).  *pr_url* is only populated when the
             action is ``"fix_locally"`` and the PR is created successfully.
    """
    manifest = worker.manifest
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id

    # WOR-286: Trust the worker's in-band success signal (result.json) over
    # the subprocess exit code. Claude Code CLI sometimes exits non-zero
    # during teardown after a fully successful run — that should not destroy
    # the work. Only treat non-zero exit as failure when result.json is
    # missing or also reports failure.
    result_status = _read_result_status(repo_root, manifest)
    if returncode != 0:
        if result_status == "success":
            logger.warning(
                "Worker %s exited non-zero (%d) but result.json reports "
                "status=success — trusting in-band signal and proceeding "
                "with checks.",
                ticket_id,
                returncode,
            )
            # Fall through to run_checks; treat as if returncode == 0.
        else:
            logger.error(
                "Worker %s exited non-zero (%d); result.json status=%s — "
                "routing to failure path",
                ticket_id,
                returncode,
                result_status if result_status is not None else "missing",
            )
            escalated = bool(manifest.failure_policy.escalate_to_cloud)
            if escalated:
                logger.info("Escalating %s to cloud per failure policy", ticket_id)
                safe_set_state(linear, linear_id, _IN_PROGRESS_STATE, ticket_id)
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
            return "failure", escalated, False, None, [], None

    checks_ok, failed_checks = run_checks(
        manifest,
        worker.worktree_path,
        metrics=metrics,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    if not checks_ok:
        worker.retry_count += 1
    if not checks_ok and manifest.failure_policy.on_check_failure == "abort":
        escalated = bool(manifest.failure_policy.escalate_to_cloud)
        if escalated:
            logger.info("Escalating %s to cloud after check failure", ticket_id)
            safe_set_state(linear, linear_id, _IN_PROGRESS_STATE, ticket_id)
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
        return "failure", escalated, False, None, failed_checks, None

    preserve_worker_artifacts(repo_root, worker)
    flags = _read_result_flags(repo_root / manifest.artifact_paths.result_json)
    action = escalation_policy.classify_result(**flags)

    outcome, escalated, sonar_findings, pr_url = _handle_policy_outcome(
        action,
        flags,
        worker,
        linear,
        escalation_policy,
        manifest.objective,
        tracked_prs=tracked_prs,
    )
    return outcome, escalated, True, sonar_findings, [], pr_url


def _handle_policy_outcome(
    action: str,
    flags: dict[str, bool],
    worker: ActiveWorker,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
    final_message: str = "Implementation complete",
    tracked_prs: list[TrackedPR] | None = None,
) -> tuple[Outcome, bool, list[str] | None, str | None]:
    """Map a policy action to an outcome, posting Linear comments as needed.

    Returns (outcome, escalated, sonar_findings, pr_url).  *pr_url* is only
    populated when the action is ``"fix_locally"`` and the PR is created
    successfully.
    """
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id
    manifest = worker.manifest

    if action == "escalate":
        triggering = next((f for f in _POLICY_FLAGS if flags.get(f)), "unknown")
        logger.info("Escalating %s to cloud (flag=%s)", ticket_id, triggering)
        safe_set_state(linear, linear_id, _IN_PROGRESS_STATE, ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"Local worker escalating `{ticket_id}` to cloud. "
            f"Triggering flag: `{triggering}`.",
        )
        return "escalated", True, None, None

    if action == "human":
        logger.info("Human review required for %s per policy", ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"Human review required for `{ticket_id}` before "
            f"proceeding. Please inspect the result artifact.",
        )
        return "aborted", False, None, None

    # fix_locally — check Sonar findings before creating PR
    sonar_findings = fetch_sonar_findings(manifest.worker_branch)
    if _sonar_requires_escalation(sonar_findings, ticket_id, escalation_policy):
        safe_set_state(linear, linear_id, _IN_PROGRESS_STATE, ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"Local worker escalating `{ticket_id}` to cloud due "
            f"to Sonar finding requiring immediate action.",
        )
        return "escalated", True, sonar_findings, None

    # Squash any wip commits into a single commit so retry workers can
    # diff the final_message commit and resume from a clean state.
    squash_wip_commits(
        worker.worktree_path,
        ticket_id,
        manifest.base_branch,
        final_message,
    )

    outcome, pr_url = attempt_pr(manifest, worker, linear, tracked_prs=tracked_prs)
    return outcome, False, sonar_findings, pr_url


def _sonar_requires_escalation(
    sonar_findings: list[str] | None,
    ticket_id: str,
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


def _write_wip_sha_to_last_failure(
    worker: ActiveWorker,
    wip_sha: str,
) -> None:
    """Add wip_commit_sha to last_failure.json in the worktree artifact dir."""
    artifact_dir = (
        worker.worktree_path / worker.manifest.artifact_paths.result_json
    ).parent
    failure_file = artifact_dir / "last_failure.json"
    try:
        data: dict[str, object] = {}
        if failure_file.exists():
            data.update(json.loads(failure_file.read_text(encoding="utf-8")))
        data["wip_commit_sha"] = wip_sha
        failure_file.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
        logger.debug(
            "WIP commit SHA written to %s for %s",
            failure_file,
            worker.ticket_id,
        )
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not write wip_commit_sha to %s for %s: %s",
            failure_file,
            worker.ticket_id,
            exc,
        )


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
