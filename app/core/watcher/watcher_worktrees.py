"""Worktree lifecycle functions for the watcher sub-system.

All functions take repo_root as an explicit parameter — no persistent state
is needed, so a class boundary would add no value here.
This module may import from watcher_types only (no other watcher siblings).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Literal, NamedTuple

from app.core.manifest import ExecutionManifest

from .watcher_types import (
    _CLAUDE_DIR,
    _WORKTREE_BASE,
    ActiveWorker,
)

logger = logging.getLogger(__name__)


class WipPreservationResult(NamedTuple):
    """Outcome of attempting to preserve worker work-in-progress (WOR-288).

    The caller (``finalize_worker``) uses the ``status`` field to decide
    whether it is safe to remove the worktree:

    * ``"clean"``    — tree was clean, no work to preserve. Safe to cleanup.
    * ``"pushed"``   — commit + push succeeded; ``sha`` is the short SHA.
                       Safe to cleanup.
    * ``"backup"``   — commit or push failed, but the dirty worktree files
                       were copied to ``backup_path``. Safe to cleanup.
    * ``"failed"``   — neither pushed nor backed up. **Caller MUST NOT
                       cleanup** the worktree — work would be lost.

    ``error`` carries the first stderr/exception summary observed, for log
    surfacing. ``backup_path`` is set only on ``"backup"``.
    """

    status: Literal["clean", "pushed", "backup", "failed"]
    sha: str | None
    backup_path: Path | None
    error: str | None


def create_worktree(repo_root: Path, manifest: ExecutionManifest) -> Path:
    """Add a git worktree for *manifest* and rebase it onto its base branch."""
    worktree_name = manifest.worktree_name or manifest.worker_branch
    if ".." in Path(worktree_name).parts:
        raise ValueError(f"Invalid worktree name: {worktree_name!r}")
    worktree_path = repo_root.parent / _WORKTREE_BASE / worktree_name
    subprocess.run(  # nosec B603 B607
        [
            "git",
            "-C",
            str(repo_root),
            "worktree",
            "add",
            str(worktree_path),
            manifest.worker_branch,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    logger.info("Worktree created at %s", worktree_path)
    rebase_worktree_from_base(worktree_path, manifest.base_branch)
    return worktree_path


def rebase_worktree_from_base(worktree_path: Path, base_branch: str) -> None:
    """Fetch and rebase the worktree from origin/<base_branch>.

    Ensures the worker starts from the latest epic state, not a stale
    snapshot from when the branch was created.  Logs a warning on failure
    rather than raising — a stale start is preferable to no start at all.
    """
    try:
        subprocess.run(  # nosec B603 B607
            ["git", "-C", str(worktree_path), "fetch", "origin", base_branch],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "rebase",
                f"origin/{base_branch}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.debug(
            "Worktree at %s rebased onto origin/%s", worktree_path, base_branch
        )
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Could not rebase worktree onto origin/%s (worker will start from "
            "branch tip instead): %s",
            base_branch,
            (exc.stderr or exc.stdout or str(exc)).strip(),
        )


_LAST_FAILURE_FILENAME = "last_failure.json"


def copy_manifest_to_worktree(
    repo_root: Path, manifest: ExecutionManifest, worktree_path: Path
) -> None:
    """Copy the manifest JSON into the worktree artifact directory.

    Also copies last_failure.json from the repo artifact dir if it exists,
    so retry workers have context on what the previous run failed on.
    """
    src = repo_root / manifest.artifact_paths.manifest_copy
    dest = worktree_path / manifest.artifact_paths.manifest_copy
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

    failure_src = src.parent / _LAST_FAILURE_FILENAME
    if failure_src.exists():
        shutil.copy2(failure_src, dest.parent / _LAST_FAILURE_FILENAME)


def backup_plan_files() -> list[Path]:
    """Move ~/.claude/plans/*.md aside so the worker doesn't enter plan mode.

    Claude Code enters plan mode whenever it finds a plan file in the plans
    directory at startup. Workers must never enter plan mode — they run
    non-interactively and ExitPlanMode would silently terminate the session.
    Returns the list of backup paths so the caller can restore them later.
    """
    plans_dir = Path.home() / _CLAUDE_DIR / "plans"
    if not plans_dir.exists():
        return []
    backup_dir = plans_dir.parent / "plans_worker_backup"
    backup_dir.mkdir(exist_ok=True)
    moved: list[Path] = []
    for plan_file in plans_dir.glob("*.md"):
        dest = backup_dir / plan_file.name
        shutil.move(str(plan_file), dest)
        moved.append(dest)
    if moved:
        logger.debug("Backed up %d plan file(s) to %s", len(moved), backup_dir)
    return moved


def restore_plan_files(backed_up: list[Path]) -> None:
    """Restore plan files moved by backup_plan_files."""
    if not backed_up:
        return
    plans_dir = Path.home() / _CLAUDE_DIR / "plans"
    plans_dir.mkdir(exist_ok=True)
    for plan_file in backed_up:
        shutil.move(str(plan_file), plans_dir / plan_file.name)
    logger.debug("Restored %d plan file(s)", len(backed_up))


def write_worker_pytest_config(worktree_path: Path) -> None:
    """Write pytest.ini overriding pyproject.toml addopts in the worktree.

    pytest.ini takes precedence over pyproject.toml, so this strips
    --cov-fail-under from every pytest call the worker makes. Coverage
    is still enforced by CI on the PR.
    """
    (worktree_path / "pytest.ini").write_text("[pytest]\naddopts = --tb=short\n")


def _save_dirty_worktree_to_backup(
    worktree_path: Path,
    ticket_id: str,
    backup_root: Path,
) -> Path | None:
    """Copy a dirty worktree's contents to ``<backup_root>/<ticket_lower>/wip/``.

    Used when ``commit_wip_state`` cannot push the WIP commit (network down,
    branch protection, hook reject). Without this fallback, the unconditional
    ``cleanup_worktree`` call in ``finalize_worker`` would destroy the work.

    Returns the absolute backup path on success, or None on failure.
    Skips ``.git/`` and ``__pycache__/`` to keep the backup small.
    """
    target = backup_root / ticket_id.lower().replace("-", "_") / "wip"
    try:
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            worktree_path,
            target,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
    except OSError as exc:
        logger.exception(
            "Failed to back up dirty worktree for %s to %s: %s",
            ticket_id,
            target,
            exc,
        )
        return None
    logger.warning(
        "WIP push failed for %s — dirty worktree backed up to %s",
        ticket_id,
        target,
    )
    return target


def commit_wip_state(
    worktree_path: Path,
    ticket_id: str,
    worker_branch: str,
    *,
    backup_root: Path | None = None,
) -> WipPreservationResult:
    """Preserve worker work-in-progress (WOR-258, WOR-288).

    Tries to commit and push uncommitted worktree changes so a retry worker
    can resume. If push fails and ``backup_root`` is provided, falls back to
    copying the dirty worktree contents to
    ``<backup_root>/<ticket_lower>/wip/`` so the work is not lost when
    ``finalize_worker`` removes the worktree.

    Returns a :class:`WipPreservationResult` whose ``status`` tells the
    caller whether it is safe to call ``cleanup_worktree`` next:

    * ``"clean"`` / ``"pushed"`` / ``"backup"`` → safe to cleanup
    * ``"failed"`` → caller MUST leave the worktree in place

    Never raises.
    """
    error: str | None = None
    try:
        # Check if tree is clean
        status = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(worktree_path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode != 0:
            error = (status.stderr or status.stdout or "").strip()
            logger.warning(
                "git status failed in %s for %s — cannot determine WIP state: %s",
                worktree_path,
                ticket_id,
                error,
            )
            return WipPreservationResult(
                status="failed", sha=None, backup_path=None, error=error
            )
        if not status.stdout.strip():
            logger.info(
                "No working tree changes for %s — no wip commit needed",
                ticket_id,
            )
            return WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            )

        # Stage all changes
        subprocess.run(  # nosec B603 B607
            ["git", "-C", str(worktree_path), "add", "-A"],
            check=True,
            capture_output=True,
            text=True,
        )

        # Commit with the wip message
        subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "commit",
                "-m",
                f"wip(failed): {ticket_id} pre-failure state "
                "— retry may resume from here",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        # Push the branch
        subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "push",
                "-u",
                "origin",
                worker_branch,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        # Get short SHA
        rev = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(worktree_path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        sha = rev.stdout.strip()
        logger.info(
            "WIP commit %s created for %s on %s",
            sha,
            ticket_id,
            worker_branch,
        )
        return WipPreservationResult(
            status="pushed", sha=sha, backup_path=None, error=None
        )

    except subprocess.CalledProcessError as exc:
        error = (exc.stderr or exc.stdout or str(exc)).strip()
        logger.warning(
            "WIP commit/push failed for %s: %s",
            ticket_id,
            error,
        )
    except OSError as exc:
        error = str(exc)
        logger.warning("WIP commit failed for %s (OSError): %s", ticket_id, exc)

    # Fall through: commit or push failed. Try the dirty-worktree backup so
    # the work is not lost when finalize_worker removes the worktree.
    if backup_root is not None:
        backup = _save_dirty_worktree_to_backup(worktree_path, ticket_id, backup_root)
        if backup is not None:
            return WipPreservationResult(
                status="backup", sha=None, backup_path=backup, error=error
            )
    return WipPreservationResult(
        status="failed", sha=None, backup_path=None, error=error
    )


def squash_wip_commits(
    worktree_path: Path,
    ticket_id: str,
    base_branch: str,
    final_message: str,
) -> str | None:
    """Squash all wip commits since the diverge point into a single commit.

    Finds all commits whose message matches ``wip: <ticket_id>`` since the
    branch diverged from *base_branch*. If any exist, does a
    ``git reset --soft <diverge>`` followed by ``git commit -m <final_message>``
    and pushes the result.

    Returns the new short commit SHA on success, or ``None`` when no wip
    commits are found (no-op) or any git step fails.  Never raises — logs
    warnings instead.
    """
    try:
        # Find diverge point
        merge_base = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "merge-base",
                "HEAD",
                base_branch,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        diverge = merge_base.stdout.strip()

        # Check for wip commits
        log = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "log",
                "--oneline",
                f"{diverge}..HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        wip_commits: list[str] = []
        for line in log.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Match: <hex-sha> wip: <ticket_id>
            # git log --oneline: sha (8+ chars) + space + title
            sha_part = stripped[:7]
            if re.match(r"^[0-9a-f]{7,}", sha_part) and f"wip: {ticket_id}" in stripped:
                wip_commits.append(stripped)

        if not wip_commits:
            logger.info(
                "No wip commits for %s since %s — nothing to squash",
                ticket_id,
                base_branch,
            )
            return None

        logger.info(
            "Found %d wip commit(s) for %s — squashing into single commit",
            len(wip_commits),
            ticket_id,
        )

        # Squash: soft reset to diverge point
        subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "reset",
                "--soft",
                diverge,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        # Commit with final message
        subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "commit",
                "-m",
                final_message,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        # Push — the current branch (worker branch) now has the squashed commit
        push = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "push",
                "-u",
                "origin",
                "HEAD",
            ],
            capture_output=True,
            text=True,
        )
        if push.returncode != 0:
            logger.warning(
                "git push failed after squashing wip commits for %s — "
                "commit was created locally but not pushed: %s",
                ticket_id,
                (push.stderr or push.stdout or "").strip(),
            )
            return None

        # Get short SHA
        rev = subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-parse",
                "--short",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        sha = rev.stdout.strip()
        logger.info(
            "WIP commits squashed for %s — new commit %s",
            ticket_id,
            sha,
        )
        return sha

    except subprocess.CalledProcessError as exc:
        logger.warning(
            "squash_wip_commits failed for %s: %s",
            ticket_id,
            (exc.stderr or exc.stdout or str(exc)).strip(),
        )
        return None
    except OSError as exc:
        logger.warning(
            "squash_wip_commits failed for %s (OSError): %s",
            ticket_id,
            exc,
        )
        return None


def preserve_worker_artifacts(repo_root: Path, worker: ActiveWorker) -> None:
    """Copy worker log and result.json from the worktree to the repo artifact dir.

    The worktree is removed after this call, so any file not copied here is lost.
    Also handles last_failure.json: copies it on check failure, deletes the repo
    copy on successful run (when the worktree no longer contains the file).
    """
    artifact_dir = (repo_root / worker.manifest.artifact_paths.result_json).parent
    artifact_dir.mkdir(parents=True, exist_ok=True)

    log_src = worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
    if log_src.exists():
        shutil.copy2(log_src, artifact_dir / log_src.name)
        logger.info("Worker log preserved at %s", artifact_dir / log_src.name)

    result_src = worker.worktree_path / worker.manifest.artifact_paths.result_json
    if result_src.exists():
        shutil.copy2(result_src, artifact_dir / result_src.name)
        logger.info("Result artifact preserved at %s", artifact_dir / result_src.name)
    else:
        logger.warning(
            "No result artifact found at %s for %s",
            result_src,
            worker.ticket_id,
        )

    wt_failure = (
        worker.worktree_path / worker.manifest.artifact_paths.result_json
    ).parent / _LAST_FAILURE_FILENAME
    repo_failure = artifact_dir / _LAST_FAILURE_FILENAME
    if wt_failure.exists():
        shutil.copy2(wt_failure, repo_failure)
        logger.info("Failure context preserved at %s", repo_failure)
    elif repo_failure.exists():
        repo_failure.unlink()
        logger.debug("Cleared last_failure.json after successful run: %s", repo_failure)


def cleanup_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Remove a git worktree, logging a warning on failure.

    Falls back to ``shutil.rmtree`` when ``git worktree remove`` reports the
    directory is not a registered worktree — that happens when a previous
    cleanup failed mid-run, leaving a directory on disk that git no longer
    tracks.
    """
    try:
        subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "remove",
                "--force",
                str(worktree_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Worktree removed: %s", worktree_path)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        if "is not a working tree" in stderr and worktree_path.exists():
            try:
                _rmtree_force(worktree_path)
                logger.info(
                    "Untracked worktree directory removed via rmtree: %s",
                    worktree_path,
                )
                subprocess.run(  # nosec B603 B607
                    ["git", "-C", str(repo_root), "worktree", "prune"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                return
            except OSError as rmtree_exc:
                logger.warning(
                    "Failed to rmtree untracked worktree %s: %s",
                    worktree_path,
                    rmtree_exc,
                )
                return
        logger.warning("Failed to remove worktree %s: %s", worktree_path, stderr)


def _rmtree_force(path: Path) -> None:
    """``shutil.rmtree`` that handles Windows read-only files (e.g. ``.git`` entries
    inside a git worktree directory).
    """
    import os
    import stat
    from collections.abc import Callable

    def _on_rm_error(func: Callable[..., object], p: str, _exc_info: object) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=_on_rm_error)


def cleanup_orphaned_worktrees(repo_root: Path) -> None:
    """Remove any leftover watcher-managed worktrees from a prior run."""
    base = repo_root.parent / _WORKTREE_BASE
    if not base.exists():
        return
    for worktree_dir in base.iterdir():
        if not worktree_dir.is_dir():
            continue
        logger.warning("Orphaned worktree detected: %s — removing", worktree_dir)
        cleanup_worktree(repo_root, worktree_dir)


def cleanup_stale_artifacts(
    artifact_dir: Path,
    ticket_id: str,
) -> list[str]:
    """Archive or remove stale result.json / worker logs before re-dispatch.

    When a ticket is re-dispatched after a prior failure (Blocked → ReadyForLocal),
    leftover ``result.json`` and ``worker_*.log`` files from the previous run must
    be removed so they do not leak stale data into the new worktree.

    Returns a list of file paths that were cleaned up, for logging.
    """
    cleaned: list[str] = []

    # Remove stale result.json
    result_path = artifact_dir / "result.json"
    if result_path.exists():
        logger.warning("Removing stale result.json for %s — %s", ticket_id, result_path)
        result_path.unlink()
        cleaned.append(str(result_path))

    # Remove stale worker logs (worker_<ticket>.log)
    log_prefix = f"worker_{ticket_id.lower()}"
    if artifact_dir.is_dir():
        for child in sorted(artifact_dir.iterdir()):
            if child.is_file() and child.name.startswith(log_prefix):
                logger.warning("Removing stale worker log %s — %s", ticket_id, child)
                child.unlink()
                cleaned.append(str(child))

    return cleaned


def cleanup_orphan_dir(path: Path) -> None:
    """Remove an orphan worktree directory not tracked by ``git worktree list``.

    A directory at the expected path may persist on disk after a prior watcher run
    crashes or is killed mid-cleanup. It is not registered as a git worktree — the
    subsequent ``git worktree add`` will fail with ``already exists`` unless removed
    first.

    Uses ``shutil.rmtree(..., ignore_errors=True)`` so the caller does not need to
    handle ``OSError`` / ``PermissionError`` for locked / read-only directories.
    Logs a WARN so the operator sees what happened (audit trail, WOR-66).
    """
    if path.exists():
        logger.warning(
            "Orphan directory at %s is not a git worktree — removing via rmtree",
            path,
        )
        shutil.rmtree(path, ignore_errors=True)
