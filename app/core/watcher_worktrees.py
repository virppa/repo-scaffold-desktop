"""Worktree lifecycle functions for the watcher sub-system.

All functions take repo_root as an explicit parameter — no persistent state
is needed, so a class boundary would add no value here.
This module may import from watcher_types only (no other watcher siblings).
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec B404
from pathlib import Path

from app.core.manifest import ExecutionManifest
from app.core.watcher_types import (
    _CLAUDE_DIR,
    _WORKTREE_BASE,
    ActiveWorker,
)

logger = logging.getLogger(__name__)


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
    """Remove a git worktree, logging a warning on failure."""
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
        logger.warning("Failed to remove worktree %s: %s", worktree_path, exc.stderr)


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
