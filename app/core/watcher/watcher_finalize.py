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
from typing import Any

from app.core.escalation_policy import EscalationPolicy, get_waste_warn_threshold
from app.core.manifest import ExecutionManifest
from app.core.metrics import (
    MetricsStore,
    Outcome,
    TicketMetrics,
    TicketRunLog,
    compute_tags,
)

from .watcher_finalize_helpers import (
    ATTEMPT_HARDCAP,
    _execute_finalization,
    _read_result_status,
    _try_post_comment,
    _write_wip_sha_to_last_failure,
    _write_wip_state_to_last_failure,
    safe_set_state,
)
from .watcher_helpers import (
    _parse_hook_trust_violations,
    _parse_worker_api_retries,
    _parse_worker_behavior,
    _parse_worker_subagent_spawns,
    _parse_worker_usage,
    _read_result_flags,
    capture_vllm_metrics,
    compute_vllm_metrics_delta,
    resolve_effective_mode,
)
from .watcher_subprocess import (
    create_pr,
    launch_worker,
    parse_git_shortstat,
)
from .watcher_tui import TrackedPR
from .watcher_types import (
    _CLAUDE_DIR,
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
)
from .worker_waste import compute_waste_score

__all__ = ["attempt_pr", "finalize_worker", "safe_set_state"]

logger = logging.getLogger(__name__)

# Minimum character threshold for result.json `notes` to qualify for
# auto-posting to the WOR-254 improvement log. Tune here before adding a
# config knob (WOR-303).
NOTES_MIN_CHARS: int = 50

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


def _write_vllm_metrics_artifact(
    repo_root: Path,
    ticket_id: str,
    *,
    attributable: bool,
    before: dict[str, float] | None,
    after: dict[str, float] | None,
    deltas: dict[str, float | int | None],
    reason: str | None = None,
) -> None:
    """Write the WOR-370 vLLM /metrics audit artifact for one session.

    Goes to ``.claude/artifacts/<ticket>/vllm_metrics.json`` next to the
    existing manifest copy. Failure is non-fatal — finalize_worker should
    not crash on disk write errors.
    """
    slug = ticket_id.lower().replace("-", "_")
    artifact_dir = repo_root / ".claude" / "artifacts" / slug
    payload: dict[str, object] = {
        "ticket_id": ticket_id,
        "attributable": attributable,
    }
    if reason is not None:
        payload["reason"] = reason
    if before is not None:
        payload["before"] = before
    if after is not None:
        payload["after"] = after
    if deltas:
        payload["deltas"] = deltas
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "vllm_metrics.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "Could not write vLLM metrics artifact for %s: %s", ticket_id, exc
        )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


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


def _run_retry_loop(
    worker: ActiveWorker,
    returncode: int,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
    repo_root: Path,
    eff: str,
    tracked_prs: list[TrackedPR] | None,
    metrics: MetricsStore,
    project_id: str,
) -> tuple[Outcome, bool, bool, list[str] | None, list[dict[str, int | str]]]:
    """Execute the in-dispatch retry loop (WOR-312). Returns the final
    finalization tuple after the last attempt."""
    first_finalization = True
    max_retries = min(ATTEMPT_HARDCAP, worker.manifest.failure_policy.max_retries)
    while True:
        outcome, escalated, artifacts_preserved, sonar_findings, failed_checks, _ = (
            _execute_finalization(
                worker,
                returncode,
                linear,
                escalation_policy,
                repo_root,
                attempt_pr,
                tracked_prs=tracked_prs,
                metrics=metrics,
                project_id=project_id,
            )
        )
        worker.attempt_count += 1
        if not failed_checks:
            return (
                outcome,
                escalated,
                artifacts_preserved,
                sonar_findings,
                failed_checks,
            )
        if worker.attempt_count > max_retries:
            return (
                outcome,
                escalated,
                artifacts_preserved,
                sonar_findings,
                failed_checks,
            )
        failed_checks_json = json.dumps([str(f["check"]) for f in failed_checks])
        extra_constraint = (
            f"Worker failed checks: {failed_checks_json}. Re-launching with RETRY hint."
        )
        if first_finalization:
            logger.info("%s — %s", worker.ticket_id, extra_constraint)
        else:
            logger.warning(
                "%s check retry %d/%d — checks failed",
                worker.ticket_id,
                worker.attempt_count,
                max_retries,
            )
        launch_worker(
            repo_root,
            worker.manifest,
            worker.worktree_path,
            eff,
            extra_constraint=extra_constraint,
        )
        first_finalization = False


def _compute_diff_stats(worktree_path: Path, base_branch: str) -> tuple[int, int]:
    """Run `git diff --shortstat` against the merge-base; return (lines, files)."""
    try:
        diff_output = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "diff",
                "--shortstat",
                f"{base_branch}...HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = (diff_output.stdout or "") + (diff_output.stderr or "")
        return parse_git_shortstat(raw)
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0


def _capture_vllm_post(
    worker: ActiveWorker, repo_root: Path
) -> tuple[bool | None, dict[str, float | int | None]]:
    """WOR-370: capture the post-session vLLM /metrics snapshot.

    Returns (attributable, deltas). Three states:
      - (True, deltas) — worker remained solo, after-snapshot succeeded
      - (False, {})   — concurrent during session, OR after-snapshot failed
      - (None, {})    — no pre-snapshot was taken
    """
    if worker.vllm_metrics_before is None:
        return None, {}
    if not worker.remained_solo:
        _write_vllm_metrics_artifact(
            repo_root,
            worker.ticket_id,
            attributable=False,
            before=worker.vllm_metrics_before,
            after=None,
            deltas={},
            reason="concurrent worker dispatched during session",
        )
        return False, {}
    after = capture_vllm_metrics()
    if after is None:
        _write_vllm_metrics_artifact(
            repo_root,
            worker.ticket_id,
            attributable=False,
            before=worker.vllm_metrics_before,
            after=None,
            deltas={},
            reason="after-snapshot failed (vLLM /metrics unreachable)",
        )
        return False, {}
    deltas = compute_vllm_metrics_delta(worker.vllm_metrics_before, after)
    _write_vllm_metrics_artifact(
        repo_root,
        worker.ticket_id,
        attributable=True,
        before=worker.vllm_metrics_before,
        after=after,
        deltas=deltas,
    )
    return True, deltas


def _post_improvement_log(linear: LinearClientProtocol, worker: ActiveWorker) -> None:
    """WOR-303: harvest result.json notes and post to WOR-254."""
    result_path = worker.worktree_path / worker.manifest.artifact_paths.result_json
    try:
        if not result_path.exists():
            return
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        notes = (result_data.get("notes") or "").strip()
        if len(notes) <= NOTES_MIN_CHARS:
            return
        try:
            linear.post_comment(
                "WOR-254",
                f"## Side-discovery from {worker.ticket_id}\n\n"
                f"From the {worker.ticket_id} worker session "
                f"({worker.manifest.title}):\n\n"
                f"{notes}\n\n"
                f"Ref: {worker.ticket_id}, branch "
                f"`{worker.manifest.worker_branch}`.",
            )
        except Exception:
            logger.warning(
                "Could not post improvement-log comment for %s", worker.ticket_id
            )
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not read result.json for improvement-log harvest: %s", exc
        )


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
    eff = resolve_effective_mode(mode, worker.manifest.implementation_mode)

    # WOR-312: in-dispatch retry loop. Helper avoids the slower Linear
    # Blocked -> ReadyForLocal redispatch when checks transiently fail.
    outcome, escalated, artifacts_preserved, sonar_findings, failed_checks = (
        _run_retry_loop(
            worker,
            returncode,
            linear,
            escalation_policy,
            repo_root,
            eff,
            tracked_prs,
            metrics,
            project_id,
        )
    )

    log_path = worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
    input_tokens, output_tokens, context_compactions, compact_duration_ms = (
        _parse_worker_usage(log_path)
    )
    api_retry_count = _parse_worker_api_retries(log_path)
    subagent_spawns = _parse_worker_subagent_spawns(log_path)
    hook_trust_violations = _parse_hook_trust_violations(log_path)
    _warn_hook_trust(worker.ticket_id, hook_trust_violations)
    # WOR-380: per-worker behavior telemetry. Concurrency-safe — extracted
    # from this worker's own log file.
    behavior = _parse_worker_behavior(log_path)
    tool_breakdown_json: str | None = (
        json.dumps(behavior.tool_calls_breakdown, sort_keys=True)
        if behavior.tool_calls_breakdown is not None
        else None
    )

    # WOR-354: 3-dot diff against merge-base. Helper to keep finalize_worker tight.
    lines_changed, files_changed = _compute_diff_stats(
        worker.worktree_path, worker.manifest.base_branch
    )

    cost = _build_cost_metrics(eff, input_tokens, output_tokens, wall_time)

    # Compute waste score from the worker log (WOR-277).
    waste_report = compute_waste_score(log_path)
    waste_score: int | None = waste_report.score if waste_report.score > 0 else None
    waste_breakdown_json: str | None = (
        json.dumps(waste_report.breakdown) if waste_report.score > 0 else None
    )
    _warn_high_waste(worker.ticket_id, waste_score, waste_report)

    # failed_check: store JSON array of all failed check names (WOR-312)
    failed_check_json: str | None = (
        json.dumps([str(f["check"]) for f in failed_checks]) if failed_checks else None
    )
    # check_failures: store the list when non-empty, else None
    check_failures = failed_checks if failed_checks else None

    # WOR-370: vLLM /metrics delta capture (3-state helper).
    vllm_attributable, vllm_deltas = _capture_vllm_post(worker, repo_root)

    metrics.record(
        TicketMetrics(
            ticket_id=worker.ticket_id,
            project_id=project_id,
            epic_id=worker.manifest.epic_id,
            implementation_mode=_to_metrics_mode(eff),
            local_used=(eff == "local"),
            local_model=cost["local_model"],
            cloud_used=(eff == "cloud"),
            cloud_model=cost["cloud_model"],
            cloud_tokens=cost["cloud_tokens"],
            cloud_cost_estimate=cost["cloud_cost_estimate"],
            local_input_tokens=cost["local_input_tokens"],
            local_output_tokens=cost["local_output_tokens"],
            local_tokens=cost["local_tokens"],
            local_wall_time=wall_time,
            output_tokens_per_wall_second=cost["output_tokens_per_wall_second"],
            escalated_to_cloud=escalated,
            outcome=outcome,
            retry_count=worker.attempt_count - 1,
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
            # WOR-274: count of manual quality-check Bash invocations during step 3
            hook_trust_violations=hook_trust_violations,
            # WOR-363: persist dispatch-time worker pool size
            dispatch_concurrency=worker.dispatch_concurrency,
            # WOR-370: vLLM /metrics deltas (None unless solo throughout)
            vllm_metrics_attributable=vllm_attributable,
            vllm_prefix_cache_hits=vllm_deltas.get("prefix_cache_hits"),  # type: ignore[arg-type]
            vllm_prefix_cache_queries=vllm_deltas.get("prefix_cache_queries"),  # type: ignore[arg-type]
            vllm_prefix_cache_hit_ratio=vllm_deltas.get("prefix_cache_hit_ratio"),
            vllm_prompt_tokens=vllm_deltas.get("prompt_tokens"),  # type: ignore[arg-type]
            vllm_generation_tokens=vllm_deltas.get("generation_tokens"),  # type: ignore[arg-type]
            vllm_ttft_seconds_sum=vllm_deltas.get("ttft_seconds_sum"),
            vllm_ttft_count=vllm_deltas.get("ttft_count"),  # type: ignore[arg-type]
            vllm_ttft_mean_seconds=vllm_deltas.get("ttft_mean_seconds"),
            vllm_preemptions=vllm_deltas.get("preemptions"),  # type: ignore[arg-type]
            # WOR-380: per-worker behavior telemetry (any concurrency)
            turn_count=behavior.turn_count,
            tool_calls_total=behavior.tool_calls_total,
            tool_calls_breakdown=tool_breakdown_json,
            thinking_blocks=behavior.thinking_blocks,
            thinking_chars_total=behavior.thinking_chars_total,
            input_tokens_max=behavior.input_tokens_max,
            input_tokens_first=behavior.input_tokens_first,
            input_tokens_last=behavior.input_tokens_last,
            redundant_reads_count=behavior.redundant_reads_count,
        )
    )

    _post_improvement_log(linear, worker)

    metrics.record_run(
        TicketRunLog(
            ticket_id=worker.ticket_id,
            attempt=worker.attempt_count + 1,
            implementation_mode=_to_metrics_mode(eff),
            outcome=outcome,
            failed_check=failed_check_json,
            wall_time_s=wall_time,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_tok_per_s=cost["output_tokens_per_wall_second"],
            context_compactions=context_compactions,
            same_epic_pair=worker.same_epic_pair,
        )
    )

    _apply_auto_tags(metrics, worker, project_id, repo_root, tracked_prs)

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
    _handle_wip_result(wip_result, worker)

    if not artifacts_preserved:
        # Failure path: also preserve full worker artifacts (logs, last_failure,
        # etc.) for forensic analysis. Success path doesn't need this — the PR
        # itself is the artifact.
        preserve_worker_artifacts(repo_root, worker)

    if wip_result.status in ("clean", "pushed", "backup"):
        cleanup_worktree(repo_root, worker.worktree_path)
    # else: status == "failed" — WOR-288: WIP preservation failed (commit
    # could neither push nor back up). Leaving the worktree in place for
    # human salvage. The ERROR log at the call site above already surfaces
    # the path and error for manual recovery.
    return outcome


def _warn_hook_trust(ticket_id: str, hook_trust_violations: int | None) -> None:
    """WOR-274: warn when the worker manually re-ran quality checks outside hooks.

    Threshold > 1 because a single accidental re-run may be harmless; repeated
    re-runs indicate systematic distrust of the hook infrastructure.
    """
    if hook_trust_violations is None or hook_trust_violations <= 1:
        return
    logger.warning(
        "hook-trust violation: %s ran quality-check tools manually %d times",
        ticket_id,
        hook_trust_violations,
    )


def _build_cost_metrics(
    eff: str,
    input_tokens: int | None,
    output_tokens: int | None,
    wall_time: float,
) -> dict[str, Any]:
    """Compute cloud + local cost/token fields based on effective mode."""
    cost: dict[str, Any] = {
        "cloud_model": None,
        "cloud_tokens": None,
        "cloud_cost_estimate": None,
        "local_model": None,
        "local_input_tokens": None,
        "local_output_tokens": None,
        "local_tokens": None,
        "output_tokens_per_wall_second": None,
    }
    if eff == "cloud" and input_tokens is not None and output_tokens is not None:
        cost["cloud_model"] = _resolve_cloud_model()
        cost["cloud_tokens"] = input_tokens + output_tokens
        cost["cloud_cost_estimate"] = _estimate_cloud_cost(
            input_tokens, output_tokens, cost["cloud_model"]
        )
        return cost
    if eff != "local":
        return cost
    cost["local_model"] = _LOCAL_MODEL
    cost["local_input_tokens"] = input_tokens
    cost["local_output_tokens"] = output_tokens
    if input_tokens is not None and output_tokens is not None:
        cost["local_tokens"] = input_tokens + output_tokens
    if output_tokens is not None and wall_time and wall_time > 0:
        cost["output_tokens_per_wall_second"] = output_tokens / wall_time
    return cost


def _warn_high_waste(
    ticket_id: str, waste_score: int | None, waste_report: Any
) -> None:
    """WOR-277: surface a warning when the waste score exceeds the threshold."""
    if waste_score is None or waste_score <= get_waste_warn_threshold():
        return
    top_reasons = ", ".join(
        f"{k}={v}"
        for k, v in sorted(
            waste_report.breakdown.items(), key=lambda x: x[1], reverse=True
        )
        if v > 0
    )
    logger.warning(
        "High waste score for %s: %d/100 (%s)", ticket_id, waste_score, top_reasons
    )


def _handle_wip_result(wip_result: Any, worker: ActiveWorker) -> None:
    """Log + persist outcome of commit_wip_state per WIP status."""
    if wip_result.status == "backup":
        logger.warning(
            "WIP backup for %s — commit or push failed, dirty worktree saved to %s",
            worker.ticket_id,
            wip_result.backup_path,
        )
    elif wip_result.status == "failed":
        logger.error(
            "WIP preservation failed for %s — leaving worktree in place at %s "
            "(error: %s). Run `git -C <path> status` to inspect, then commit + push "
            "to %s manually.",
            worker.ticket_id,
            worker.worktree_path,
            wip_result.error or "unknown",
            worker.manifest.worker_branch,
        )
    if wip_result.sha is not None:
        _write_wip_sha_to_last_failure(worker, wip_result.sha)
    _write_wip_state_to_last_failure(worker, wip_result.status, wip_result.backup_path)


def _apply_auto_tags(
    metrics: MetricsStore,
    worker: ActiveWorker,
    project_id: str,
    repo_root: Path,
    tracked_prs: list[TrackedPR] | None,
) -> None:
    """WOR-332: compute and persist auto-tags from the metrics row + result.json."""
    manifest = worker.manifest
    row = metrics.get_by_ticket(worker.ticket_id, project_id)
    if row is None:
        return
    tags = compute_tags(
        row,
        _read_result_status(repo_root, manifest) or "",
        _read_result_flags(repo_root / manifest.artifact_paths.result_json),
        tracked_prs,
    )
    if tags:
        metrics.set_tags(worker.ticket_id, project_id, tags)
