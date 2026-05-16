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
from datetime import datetime, timezone
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


def _validate_checks_passed(
    manifest: ExecutionManifest,
    repo_root: Path,
) -> list[str]:
    """Cross-check the worker's self-reported checks against required_checks.

    Workers sometimes write ``status: success`` to result.json with a
    ``checks_passed`` list populated from *pre-commit hook names*
    (``ruff``, ``ruff-format``, ``bandit`` …) instead of the manifest's
    ``required_checks`` command strings (``mypy app/``, ``pytest`` …) —
    see WOR-456 / WOR-66. The worker ran pre-commit, saw it pass, and
    self-reported success without ever invoking the contract checks.

    Returns the list of ``required_checks`` entries the worker did NOT
    report as passed (exact string match). Empty list = no violation.

    This gate fires *only* when the worker made a **non-empty**
    ``checks_passed`` claim that fails to cover ``required_checks`` — the
    exact WOR-456 / WOR-66 bug (worker lists pre-commit hook names instead
    of the contract checks). It deliberately does NOT fire when:

    - ``required_checks`` is empty (nothing to enforce), or
    - ``result.json`` is unreadable (the returncode path's job), or
    - ``checks_passed`` is absent or empty.

    A worker that makes no checks claim at all is a different situation
    from one that makes a *wrong* claim — the empty case is covered by
    the returncode logic and the WOR-353 unscoped-pytest soft rule plus
    the watcher's own ``run_checks`` sweep, not by this contract gate.
    """
    if not manifest.required_checks:
        return []
    result_path = repo_root / manifest.artifact_paths.result_json
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw_passed = data.get("checks_passed") if isinstance(data, dict) else None
    if isinstance(raw_passed, list):
        passed = {c for c in raw_passed if isinstance(c, str)}
    else:
        passed = set()
    if not passed:
        return []
    return [rc for rc in manifest.required_checks if rc not in passed]


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


# WOR-469: Module-level executor for parallelising independent Linear API
# writes (set_state + post_comment on the same issue). Each call is a
# blocking ~300-800ms HTTP roundtrip; firing them in parallel halves the
# wait. Separate from the WOR-465 Sonar executor and the WOR-451 finalize
# executor to keep failure domains isolated.
_linear_executor: ThreadPoolExecutor | None = None


def _get_linear_executor() -> ThreadPoolExecutor:
    """Lazy-init the Linear-API executor on first use."""
    global _linear_executor
    if _linear_executor is None:
        _linear_executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="watcher-linear"
        )
    return _linear_executor


def safe_set_state_and_comment(
    linear: LinearClientProtocol,
    linear_id: str,
    state: str,
    ticket_id: str,
    body: str,
) -> None:
    """Fire safe_set_state and _try_post_comment concurrently (WOR-469).

    Linear is consistent within an issue but doesn't require ordering
    between a state transition and a comment post — both are independent
    writes on the same issue. Running them in parallel halves the
    blocking Linear API wait from ~600-1600ms to ~300-800ms per pair.

    Errors in either call are swallowed by the wrapped helpers (their
    existing semantics — neither raises). Both calls always complete
    before this function returns.
    """
    ex = _get_linear_executor()
    f1 = ex.submit(safe_set_state, linear, linear_id, state, ticket_id)
    f2 = ex.submit(_try_post_comment, linear, linear_id, ticket_id, body)
    f1.result()
    f2.result()


def _read_result_status(
    repo_root: Path,
    manifest: ExecutionManifest,
    worktree_path: Path | None = None,
) -> str | None:
    """Return the worker's self-reported status from result.json, or None.

    Workers write a ``status`` field of ``"success"`` or ``"failure"`` (or
    similar) to ``result.json`` immediately before exiting. This is the
    in-band signal of how the work actually went, distinct from the OS-level
    subprocess exit code (which can be non-zero even after a successful run
    due to Claude Code CLI teardown quirks — see WOR-286).

    WOR-501: a worker that ``git add -f``'d result.json into its branch
    leaves it only at ``worktree_path/<result_json>`` — the normally
    gitignored main-repo path stays empty, so the watcher would read
    nothing and wrongly route a sound run to failure. When *worktree_path*
    is given and the main-repo copy yields no status, fall back to the
    worktree copy before declaring "no in-band signal". The main-repo
    path keeps precedence (it is the canonical location).

    Returns None when no readable result.json with a string ``status`` is
    found in either location.
    """
    for base in (repo_root, worktree_path):
        if base is None:
            continue
        result_path = base / manifest.artifact_paths.result_json
        try:
            raw = result_path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        status = data.get("status") if isinstance(data, dict) else None
        if isinstance(status, str):
            return status
    return None


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
        # WOR-469: state + comment are independent on the same issue;
        # run concurrently to halve the Linear API wait.
        safe_set_state_and_comment(
            linear,
            linear_id,
            _IN_PROGRESS_STATE,
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
    result_status = _read_result_status(repo_root, manifest, worker.worktree_path)
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
        # WOR-469: parallelise the independent set_state + comment writes.
        safe_set_state_and_comment(
            linear,
            linear_id,
            _IN_PROGRESS_STATE,
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
        # WOR-469: parallelise the independent set_state + comment writes.
        safe_set_state_and_comment(
            linear,
            linear_id,
            _IN_PROGRESS_STATE,
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


# WOR-457: known finalize stages for the last_failure.json `stage`
# discriminator. `check` is retained for backward-compat (watcher.py's
# retry-hint reader does `data.get("check", "unknown")`).
_LAST_FAILURE_FILENAME = "last_failure.json"

_FINALIZE_STAGES = (
    "run_checks",
    "rebase",
    "push",
    "pr_create",
    "pr_merge",
    "validate_allowed_paths",
    "validate_checks_passed",
    "parse",
    "other",
)


def _artifact_dir_for(worker: ActiveWorker) -> Path:
    """The worktree artifact dir holding result.json / last_failure.json."""
    return (worker.worktree_path / worker.manifest.artifact_paths.result_json).parent


def _main_artifact_dir_for(repo_root: Path, worker: ActiveWorker) -> Path:
    """Operator-visible (main-repo) artifact dir — survives worktree cleanup.

    WOR-501: the two pre-retry validation gates (validate_allowed_paths,
    validate_checks_passed) ``return "failure"`` *before* the
    artifact-preservation step that copies the worktree dir out, so a
    last_failure.json written via :func:`_artifact_dir_for` (worktree)
    is invisible to the operator and lost on cleanup. Those gates must
    write here — the same main-repo path :func:`_read_result_status`
    and the operator look in.
    """
    return (repo_root / worker.manifest.artifact_paths.result_json).parent


def _classify_stage(exc: BaseException) -> str:
    """Best-effort map an exception to a finalize stage string (WOR-457)."""
    if isinstance(exc, json.JSONDecodeError):
        return "parse"
    parts: list[str] = [type(exc).__name__.lower(), str(exc).lower()]
    if isinstance(exc, subprocess.CalledProcessError):
        cmd = exc.cmd
        parts.append(
            cmd.lower()
            if isinstance(cmd, str)
            else " ".join(str(c) for c in (cmd or [])).lower()
        )
    blob = " ".join(parts)
    if "rebase" in blob:
        return "rebase"
    if "pr create" in blob or "gh pr create" in blob:
        return "pr_create"
    if "pr merge" in blob or "auto-merge" in blob:
        return "pr_merge"
    if "push" in blob:
        return "push"
    if any(c in blob for c in ("pytest", "mypy", "ruff", "lint-imports")):
        return "run_checks"
    if "json" in blob or "parse" in blob or "decode" in blob:
        return "parse"
    return "other"


def _record_failure_artifact(
    artifact_dir: Path,
    stage: str,
    *,
    exception: BaseException | None = None,
    check: str | None = None,
    stdout: str = "",
    stderr: str = "",
    keep_existing_stage: bool = False,
) -> None:
    """Write/merge last_failure.json for ANY finalize failure (WOR-457).

    Before WOR-457, last_failure.json was written only by the run_checks
    path (watcher_subprocess). Failures at rebase / push / pr_create /
    pr_merge / validation gates / parse left the operator with a Linear
    "Blocked" and no diagnostic artifact (WOR-441 incident).

    `stage` is the new discriminator. `check` is kept (optional) for
    backward-compat with parsers that read it. The file is *merged* into
    any existing last_failure.json so wip_status / wip_commit_sha / the
    run_checks `check` + stdout already written are preserved (the WOR-66
    artifact round-trips: existing {check:"pytest", stdout:…} gains
    {stage:"run_checks", failed_at:…}).

    Best-effort — never raises; a diagnostics-write failure must not mask
    the original error.
    """
    failure_file = artifact_dir / _LAST_FAILURE_FILENAME
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, object] = {}
        if failure_file.exists():
            try:
                existing = json.loads(failure_file.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    data.update(existing)
            except (json.JSONDecodeError, OSError):
                data = {}
        # First-failure timestamp wins: a later stage-refinement / wip
        # merge must not reset failed_at (preserves an already-recorded
        # failure time across the multiple writers in one finalize run).
        data.setdefault("failed_at", datetime.now(timezone.utc).isoformat())
        # keep_existing_stage: a generic catch-all ("other") must not
        # downgrade a more specific stage a prior writer already set
        # (e.g. attempt_pr recorded "pr_create" before this runs).
        if not (keep_existing_stage and data.get("stage")):
            data["stage"] = stage
        if check is not None:
            data["check"] = check
        elif "check" not in data:
            data["check"] = None
        if stdout:
            data["stdout"] = stdout
        if stderr:
            data["stderr"] = stderr
        if exception is not None:
            data["exception"] = f"{type(exception).__name__}: {exception}"
        failure_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write last_failure.json (%s): %s", failure_file, exc)


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
    failure_file = artifact_dir / _LAST_FAILURE_FILENAME
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
    failure_file = artifact_dir / _LAST_FAILURE_FILENAME
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
