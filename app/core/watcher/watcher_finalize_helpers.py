"""Internal helpers for worker finalization logic.

Extracted from watcher_finalize.py to reduce its LOC toward the ≤700 target.
All functions are internal to the watcher — callers in other modules import
them through the re-exports in watcher_finalize.py.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import subprocess  # nosec B404
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

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

# WOR-Sonar-tangle: callback type for `attempt_pr` injected by watcher_finalize.
# Using Callable[..., ...] (not a Protocol) keeps the type loose enough to accept
# the existing function without forcing a public-API change.
AttemptPrFn = Callable[..., "tuple[Outcome, str | None]"]

logger = logging.getLogger(__name__)

# WOR-312: Maximum number of retries per dispatch, regardless of manifest
# failure_policy.max_retries. A value of 1 means: initial attempt + 1 retry.
# Higher values from manifest are silently capped.
ATTEMPT_HARDCAP = 1

# WOR-465: Dedicated thread pool for fetching SonarCloud findings in parallel
# with run_checks. Separate from WOR-451's _finalize_executor so a Sonar
# fetch cannot deadlock waiting for a finalize-thread slot. Module-level
# singleton; 8 workers is plenty for the WOR-451 default of 4 concurrent
# finalizes (each fires one Sonar fetch) and well within SonarCloud's
# free-tier ~60 req/min ceiling.
_sonar_executor: ThreadPoolExecutor | None = None


def _get_sonar_executor() -> ThreadPoolExecutor:
    """Lazy-init the Sonar-fetch executor on first use."""
    global _sonar_executor
    if _sonar_executor is None:
        _sonar_executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="watcher-sonar"
        )
    return _sonar_executor


def _validate_allowed_paths(
    manifest: ExecutionManifest,
    worktree_path: Path,
) -> list[str]:
    """Validate the worker diff against allowed_paths and forbidden_paths.

    Runs ``git diff --name-only <base_branch>...HEAD`` inside the worktree,
    then matches each changed file against the manifest's ``allowed_paths``
    and ``forbidden_paths`` globs using ``fnmatch``.

    Returns a list of violation strings.  Empty list means the diff is clean
    and the PR path is safe to continue.  Each violation is tagged with
    ``ALLOWED`` or ``FORBIDDEN`` for easy display in a Linear comment.
    """
    violations: list[str] = []

    # Get the list of changed files since the merge-base.
    try:
        diff_proc = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "diff",
                "--name-only",
                f"{manifest.base_branch}...HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        files = (diff_proc.stdout or "").strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        # If we can't read the diff, allow it — a git error is not a scope
        # violation. The finalize checks (ruff/mypy/pytest) will still catch
        # most problems.
        return []

    # Check against forbidden_paths first (they override).
    if manifest.forbidden_paths:
        for fpath in files:
            for pattern in manifest.forbidden_paths:
                if fnmatch.fnmatch(fpath, pattern):
                    violations.append(f"FORBIDDEN {fpath}")
                    break  # one match per file is enough

    # Check against allowed_paths.  Empty list = no restriction (anything goes).
    if manifest.allowed_paths:
        for fpath in files:
            if any(fnmatch.fnmatch(fpath, pat) for pat in manifest.allowed_paths):
                continue  # file matches an allowed pattern
            violations.append(f"ALLOWED {fpath}")

    return violations


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


def _record_failure_state(
    worker: ActiveWorker,
    linear: LinearClientProtocol,
    escalate_reason: str,
) -> bool:
    """Set Linear state for a failed worker; return whether we escalated."""
    manifest = worker.manifest
    ticket_id = worker.ticket_id
    linear_id = worker.linear_id
    escalated = bool(manifest.failure_policy.escalate_to_cloud)
    if escalated:
        logger.info("Escalating %s to cloud per failure policy", ticket_id)
        safe_set_state(linear, linear_id, _IN_PROGRESS_STATE, ticket_id)
        _try_post_comment(
            linear,
            linear_id,
            ticket_id,
            f"Local worker failed for `{ticket_id}` ({escalate_reason}). "
            f"Escalating to cloud per failure policy.",
        )
    else:
        safe_set_state(linear, linear_id, manifest.ticket_state_map.failed, ticket_id)
    return escalated


def _execute_finalization(
    worker: ActiveWorker,
    returncode: int,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
    repo_root: Path,
    attempt_pr_fn: AttemptPrFn,
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

    # WOR-286: Trust the worker's in-band success signal (result.json) over
    # the subprocess exit code. Claude Code CLI sometimes exits non-zero
    # during teardown after a fully successful run — that should not destroy
    # the work. Only treat non-zero exit as failure when result.json is
    # missing or also reports failure.
    result_status = _read_result_status(repo_root, manifest)
    if returncode != 0 and result_status != "success":
        logger.error(
            "Worker %s exited non-zero (%d); result.json status=%s — "
            "routing to failure path",
            ticket_id,
            returncode,
            result_status if result_status is not None else "missing",
        )
        escalated = _record_failure_state(
            worker,
            linear,
            escalate_reason="non-zero exit",
        )
        return "failure", escalated, False, None, [], None
    if returncode != 0:
        logger.warning(
            "Worker %s exited non-zero (%d) but result.json reports "
            "status=success — trusting in-band signal and proceeding with checks.",
            ticket_id,
            returncode,
        )

    # WOR-465: fire Sonar fetch concurrent with run_checks. Sonar typically
    # takes 5-15s; run_checks 30-125s. By the time checks return the future
    # is usually done, saving ~10s/finalize on the fix_locally path. On
    # escalate/human action paths the future result is discarded (wasted
    # ~10s of background HTTP — acceptable for the common-case win).
    sonar_future = _get_sonar_executor().submit(
        fetch_sonar_findings, manifest.worker_branch
    )

    checks_ok, failed_checks = run_checks(
        manifest,
        worker.worktree_path,
        metrics=metrics,
        ticket_id=ticket_id,
        project_id=project_id,
    )
    if not checks_ok and manifest.failure_policy.on_check_failure == "abort":
        escalated = _record_failure_state(
            worker,
            linear,
            escalate_reason="failed checks",
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
        attempt_pr_fn,
        manifest.objective,
        tracked_prs=tracked_prs,
        sonar_future=sonar_future,
    )
    return outcome, escalated, True, sonar_findings, [], pr_url


def _handle_policy_outcome(
    action: str,
    flags: dict[str, bool],
    worker: ActiveWorker,
    linear: LinearClientProtocol,
    escalation_policy: EscalationPolicy,
    attempt_pr_fn: AttemptPrFn,
    final_message: str = "Implementation complete",
    tracked_prs: list[TrackedPR] | None = None,
    sonar_future: "Future[list[str] | None] | None" = None,
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

    # fix_locally — check Sonar findings before creating PR.
    # WOR-465: when a pre-fired Sonar future is available, join it
    # (it was started before run_checks and is usually already done).
    # Fall back to the synchronous call for older code paths that didn't
    # fire one — keeps the public API back-compat for tests/direct callers.
    if sonar_future is not None:
        try:
            sonar_findings = sonar_future.result()
        except Exception:
            sonar_findings = None
    else:
        sonar_findings = fetch_sonar_findings(manifest.worker_branch)
    if sonar_findings is None:
        # Immediate fetch failed — mark for async retry in the poll loop
        worker.pending_sonar_fetch = True
        worker.sonar_fetch_attempts = 1
        worker.sonar_first_attempted_at = time.monotonic()
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

    outcome, pr_url = attempt_pr_fn(manifest, worker, linear, tracked_prs=tracked_prs)
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


def _write_wip_state_to_last_failure(
    worker: ActiveWorker,
    status: str,
    backup_path: Path | None = None,
) -> None:
    """Write wip_status (and optionally wip_backup_path) to last_failure.json.

    Called on *every* commit_wip_state result so that last_failure.json always
    carries the latest WIP state.  wip_backup_path is written only when
    status=='backup'.
    """
    artifact_dir = (
        worker.worktree_path / worker.manifest.artifact_paths.result_json
    ).parent
    artifact_dir.mkdir(parents=True, exist_ok=True)
    failure_file = artifact_dir / "last_failure.json"
    try:
        data: dict[str, object] = {}
        if failure_file.exists():
            data.update(json.loads(failure_file.read_text(encoding="utf-8")))
        data["wip_status"] = status
        if status == "backup" and backup_path is not None:
            data["wip_backup_path"] = str(backup_path)
        failure_file.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Could not write wip_state to %s for %s: %s",
            failure_file,
            worker.ticket_id,
            exc,
        )


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
