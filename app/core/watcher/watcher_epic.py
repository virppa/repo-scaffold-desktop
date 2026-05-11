"""Epic-completion detection and announcement (extracted from watcher.py).

When the watcher's worker pools drain AND there are no more ReadyForLocal
tickets AND no manifests in WaitingForDeps, the current epic is complete.
This module decides when to announce that, what to log, and whether to
exit the daemon afterwards.

Functions are module-level and take their dependencies as parameters —
no class state. Watcher.run delegates to `check_epic_completion`.
"""

from __future__ import annotations

import logging
import subprocess  # nosec B404
from pathlib import Path
from typing import Protocol, Sequence

from app.core.manifest import ExecutionManifest
from app.core.watcher.watcher_types import (
    _CLAUDE_DIR,
    ActiveWorker,
    LinearClientProtocol,
)


class _ProcessedTicketLike(Protocol):
    """Structural type covering what watcher_epic reads from processed tickets."""

    ticket_id: str
    epic_id: str | None
    succeeded: bool
    worker_branch: str
    elapsed: float


logger = logging.getLogger(__name__)

_MANIFEST_GLOB = "*/manifest.json"


def no_remaining_ready_or_waiting(
    linear: LinearClientProtocol,
    repo_root: Path,
) -> bool:
    """Return True if there are no ReadyForLocal tickets AND no
    WaitingForDeps manifests outstanding."""
    try:
        ready = linear.list_ready_for_local()
    except Exception as exc:
        logger.warning("Epic completion check: Linear poll failed: %s", exc)
        return False
    if ready:
        return False
    artifacts = repo_root / _CLAUDE_DIR / "artifacts"
    if artifacts.exists():
        for mp in artifacts.glob(_MANIFEST_GLOB):
            try:
                if ExecutionManifest.from_json(mp).status == "WaitingForDeps":
                    return False
            except Exception as exc:
                logger.warning("Could not read manifest at %s: %s", mp, exc)
    return True


def epic_dedup_state_key(
    processed_tickets: Sequence[_ProcessedTicketLike],
) -> tuple[str | None, str]:
    """Return (epic_id, state_key) for dedup of the epic-complete announcement."""
    epic_id = next((t.epic_id for t in processed_tickets if t.epic_id), None)
    state_key = (
        "|".join(sorted(t.ticket_id for t in processed_tickets))
        + ":"
        + str(any(not t.succeeded for t in processed_tickets))
    )
    return epic_id, state_key


def lookup_pr_url(repo_root: Path, branch: str) -> str:
    """Return the PR URL for `branch` via `gh pr list`, or a fallback."""
    try:
        cmd = [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--json",
            "url",
            "--jq",
            ".[0].url",
        ]
        result = subprocess.run(  # nosec B603 B607
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
            check=False,
        )
        pr_url = result.stdout.strip()
        return pr_url if pr_url else "(not found)"
    except Exception:
        return "(not found)"


def log_epic_summary(
    processed_tickets: Sequence[_ProcessedTicketLike], repo_root: Path
) -> None:
    """Log the per-ticket summary table for processed tickets."""
    failed = [t for t in processed_tickets if not t.succeeded]
    succeeded = [t for t in processed_tickets if t.succeeded]
    if failed:
        logger.warning(
            "All tickets processed - %d failed, %d succeeded",
            len(failed),
            len(succeeded),
        )
    else:
        logger.info("All sub-tickets processed - epic complete")
    logger.info("%-15s  %-55s  %s", "Ticket", "PR URL", "Elapsed")
    for t in processed_tickets:
        pr_url = (
            "(failed)" if not t.succeeded else lookup_pr_url(repo_root, t.worker_branch)
        )
        logger.info("%-15s  %-55s  %.0fs", t.ticket_id, pr_url, t.elapsed)


def post_epic_complete_comment(linear: LinearClientProtocol, epic_id: str) -> None:
    """Post the epic-complete summary comment, swallowing transport errors."""
    try:
        linear.post_comment(
            epic_id,
            f"All sub-tickets merged — ready for `/close-epic {epic_id}`",
        )
        logger.info("Posted epic-complete comment on %s", epic_id)
    except Exception as exc:
        logger.warning("Could not post epic-complete comment on %s: %s", epic_id, exc)


def check_epic_completion(
    local_active: Sequence[ActiveWorker],
    cloud_active: Sequence[ActiveWorker],
    linear: LinearClientProtocol,
    repo_root: Path,
    processed_tickets: Sequence[_ProcessedTicketLike],
    last_announced: dict[str, str],
    no_epic_shutdown: bool,
) -> bool:
    """When pools and queue are empty, announce epic complete + maybe exit.

    Returns False if the daemon should shut down (i.e. the epic is complete
    and `no_epic_shutdown` is not set); True if it should keep running.
    Mutates ``last_announced`` to suppress duplicate announcements.
    """
    if local_active or cloud_active:
        return True
    if not no_remaining_ready_or_waiting(linear, repo_root):
        return True
    if not processed_tickets:
        return True

    epic_id, state_key = epic_dedup_state_key(processed_tickets)
    if epic_id and last_announced.get(epic_id) == state_key:
        return True
    if epic_id:
        last_announced[epic_id] = state_key

    log_epic_summary(processed_tickets, repo_root)
    failed = any(not t.succeeded for t in processed_tickets)
    if epic_id and not failed:
        post_epic_complete_comment(linear, epic_id)
    return no_epic_shutdown
