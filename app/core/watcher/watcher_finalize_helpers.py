"""Internal helpers for worker finalization logic.

Extracted from watcher_finalize.py to reduce its LOC toward the ≤700 target.
All functions are internal to the watcher — callers in other modules import
them through the re-exports in watcher_finalize.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import LinearError
from app.core.manifest import ExecutionManifest
from app.core.metrics import MetricsStore, Outcome
from app.core.watcher.watcher_helpers import (
    _POLICY_FLAGS,
    _read_result_flags,
)
from app.core.watcher.watcher_subprocess import (
    fetch_sonar_findings,
    run_checks,
)
from app.core.watcher.watcher_tui import TrackedPR
from app.core.watcher.watcher_types import (
    _IN_PROGRESS_STATE,
    ActiveWorker,
    LinearClientProtocol,
)
from app.core.watcher.watcher_worktrees import (
    preserve_worker_artifacts,
    squash_wip_commits,
)

logger = logging.getLogger(__name__)


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

    from .watcher_finalize import attempt_pr

    outcome, pr_url = attempt_pr(manifest, worker, linear, tracked_prs=tracked_prs)
    if outcome == "success":
        if manifest.base_branch == "main":
            safe_set_state(linear, linear_id, "In Review", ticket_id)
        else:
            safe_set_state(linear, linear_id, "MergedToEpic", ticket_id)
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
