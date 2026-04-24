"""Watcher / orchestrator daemon for the local worker engine.

Polls Linear for ReadyForLocal tickets, manages git worktrees, launches
claude worker sessions, collects result artifacts, runs required checks,
creates PRs, updates Linear state, and records metrics.

Usage (via CLI):
    python -m app.cli watcher [--worker-mode cloud|local]

Worker modes:
    cloud   — spawn claude with clean env (no ANTHROPIC_BASE_URL); routes to
              Anthropic API unmodified.
    local   — spawn claude --model qwen3-coder:30b via LiteLLM proxy on
              localhost:8082; auto-starts proxy if not already running.
    default — respect manifest.implementation_mode per ticket.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import NamedTuple

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import DONE_STATE_TYPES
from app.core.manifest import ExecutionManifest
from app.core.metrics import MetricsStore
from app.core.watcher_finalize import finalize_worker, safe_set_state
from app.core.watcher_helpers import check_allowed_paths_overlap, resolve_effective_mode
from app.core.watcher_services import ServiceManager
from app.core.watcher_subprocess import launch_worker
from app.core.watcher_types import (
    _CLAUDE_DIR,
    _PID_FILE,
    ActiveWorker,
    LinearClientProtocol,
)
from app.core.watcher_worktrees import (
    backup_plan_files,
    cleanup_worktree,
    copy_manifest_to_worktree,
    create_worktree,
    write_worker_pytest_config,
)

logger = logging.getLogger(__name__)


class _ProcessedTicket(NamedTuple):
    ticket_id: str
    epic_id: str | None
    worker_branch: str
    elapsed: float


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class Watcher:
    """Orchestrates local worker sessions end-to-end."""

    _POLL_INTERVAL = 30  # seconds between Linear polls

    def __init__(
        self,
        worker_mode: str = "default",
        max_local_workers: int = 1,
        max_cloud_workers: int = 3,
        linear_client: LinearClientProtocol | None = None,
        metrics_store: MetricsStore | None = None,
        repo_root: Path | None = None,
        project_id: str = "repo-scaffold-desktop",
        verbose: bool = False,
    ) -> None:
        if linear_client is None:
            from app.core.linear_client import LinearClient  # lazy import

            linear_client = LinearClient()

        self._mode = worker_mode
        self._max_local_workers = max_local_workers
        self._max_cloud_workers = max_cloud_workers
        self._linear = linear_client
        self._metrics = metrics_store or MetricsStore()
        self._repo_root = (repo_root or Path.cwd()).resolve()
        self._project_id = project_id
        self._local_active: list[ActiveWorker] = []
        self._cloud_active: list[ActiveWorker] = []
        self._processed_tickets: list[_ProcessedTicket] = []
        self._running = True
        self._services = ServiceManager(self._repo_root)
        self._verbose = verbose
        self._retry_counters: dict[str, int] = {}
        self._escalation_policy = EscalationPolicy.from_toml()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the poll loop. Blocks until SIGINT/SIGTERM."""
        self._write_pid_file()
        self._register_signals()
        self._cleanup_orphaned_worktrees()

        if self._mode == "local":
            self._services.ensure_litellm_running()

        logger.info(
            "Watcher started (mode=%s, max_local_workers=%d, max_cloud_workers=%d)",
            self._mode,
            self._max_local_workers,
            self._max_cloud_workers,
        )

        try:
            while self._running:
                self._reap_finished_workers()
                self._promote_waiting_tickets()
                local_has_capacity = len(self._local_active) < self._max_local_workers
                cloud_has_capacity = len(self._cloud_active) < self._max_cloud_workers
                if local_has_capacity or cloud_has_capacity:
                    self._dispatch_next_ticket()
                self._check_epic_completion()
                if not self._running:
                    break
                time.sleep(self._POLL_INTERVAL)
        finally:
            self._wait_for_active_workers()
            self._services.stop()
            self._remove_pid_file()
            logger.info("Watcher stopped cleanly")

    def _cleanup_orphaned_worktrees(self) -> None:
        from app.core.watcher_types import _WORKTREE_BASE

        base = self._repo_root.parent / _WORKTREE_BASE
        if not base.exists():
            return
        for worktree_dir in base.iterdir():
            if not worktree_dir.is_dir():
                continue
            logger.warning("Orphaned worktree detected: %s — removing", worktree_dir)
            cleanup_worktree(self._repo_root, worktree_dir)

    # ------------------------------------------------------------------
    # WaitingForDeps promotion
    # ------------------------------------------------------------------

    def _transition_waiting_manifest(
        self, manifest: ExecutionManifest, manifest_path: Path, new_status: str
    ) -> None:
        updated = manifest.model_copy(
            update={"status": new_status, "context_snippets": None}
        )
        updated.to_json(manifest_path)
        logger.debug(
            "Manifest for %s written with status=%s", manifest.ticket_id, new_status
        )

    def _promote_waiting_tickets(self) -> None:
        """Promote WaitingForDeps manifests to ReadyForLocal when all blockers complete.

        Scans .claude/artifacts/*/manifest.json each poll cycle. For each manifest
        with status=='WaitingForDeps', checks whether all blocked_by_tickets have
        reached a completed state in Linear. If so, writes the manifest back to disk
        with status='ReadyForLocal' and advances the Linear ticket. If any blocker
        is cancelled, posts a comment and moves the dependent ticket to Backlog.
        """
        artifacts_root = self._repo_root / _CLAUDE_DIR / "artifacts"
        if not artifacts_root.exists():
            return

        for manifest_path in sorted(artifacts_root.glob("*/manifest.json")):
            try:
                manifest = ExecutionManifest.from_json(manifest_path)
            except Exception as exc:
                logger.warning("Could not load manifest at %s: %s", manifest_path, exc)
                continue

            if manifest.status != "WaitingForDeps":
                continue

            if not manifest.blocked_by_tickets:
                logger.warning(
                    "%s has status=WaitingForDeps but no blocked_by_tickets; "
                    "promoting to ReadyForLocal",
                    manifest.ticket_id,
                )
                self._transition_waiting_manifest(
                    manifest, manifest_path, "ReadyForLocal"
                )
                self._notify_promotion(manifest)
                continue

            states = self._fetch_all_blocker_states(manifest)

            cancelled = self._find_cancelled_blocker(manifest, states)
            if cancelled is not None:
                blocker_id, state_type = cancelled
                self._handle_cancelled_predecessor(
                    manifest, manifest_path, blocker_id, state_type
                )
                continue

            if self._all_blockers_satisfied(manifest, states):
                logger.info(
                    "All blockers for %s satisfied — promoting to ReadyForLocal",
                    manifest.ticket_id,
                )
                self._transition_waiting_manifest(
                    manifest, manifest_path, "ReadyForLocal"
                )
                self._notify_promotion(manifest)

    def _fetch_all_blocker_states(
        self, manifest: ExecutionManifest
    ) -> dict[str, str | None]:
        """Snapshot all blocker states in one pass; fetch errors stored as None."""
        states: dict[str, str | None] = {}
        for blocker_id in manifest.blocked_by_tickets:
            try:
                states[blocker_id] = self._linear.get_issue_state_type(blocker_id)
            except Exception as exc:
                logger.warning(
                    "Could not fetch state for blocker %s of %s: %s",
                    blocker_id,
                    manifest.ticket_id,
                    exc,
                )
                states[blocker_id] = None
        return states

    def _find_cancelled_blocker(
        self, manifest: ExecutionManifest, states: dict[str, str | None]
    ) -> tuple[str, str] | None:
        """Return (blocker_id, state_type) for the first cancelled blocker, or None."""
        for blocker_id in manifest.blocked_by_tickets:
            state_type = states.get(blocker_id)
            if state_type == "cancelled":
                return blocker_id, state_type
        return None

    def _handle_cancelled_predecessor(
        self,
        manifest: ExecutionManifest,
        manifest_path: Path,
        blocker_id: str,
        state_type: str,
    ) -> None:
        logger.warning(
            "Blocker %s for %s is %s — moving dependent to Backlog",
            blocker_id,
            manifest.ticket_id,
            state_type,
        )
        self._transition_waiting_manifest(manifest, manifest_path, "Backlog")
        if not manifest.linear_id:
            return
        safe_set_state(self._linear, manifest.linear_id, "Backlog", manifest.ticket_id)
        try:
            msg = (
                f"Predecessor {blocker_id} moved to {state_type}"
                " — manual intervention required."
            )
            self._linear.post_comment(manifest.linear_id, msg)
        except Exception as exc:
            logger.warning(
                "Could not post predecessor-cancelled comment for %s: %s",
                manifest.ticket_id,
                exc,
            )

    def _all_blockers_satisfied(
        self, manifest: ExecutionManifest, states: dict[str, str | None]
    ) -> bool:
        for blocker_id in manifest.blocked_by_tickets:
            state_type = states.get(blocker_id)
            if state_type is None or state_type not in DONE_STATE_TYPES:
                return False
            if state_type == "cancelled":
                return False
        return True

    def _notify_promotion(self, manifest: ExecutionManifest) -> None:
        if not manifest.linear_id:
            return
        safe_set_state(
            self._linear, manifest.linear_id, "ReadyForLocal", manifest.ticket_id
        )
        try:
            self._linear.post_comment(
                manifest.linear_id,
                f"All predecessors merged. `{manifest.ticket_id}` promoted to "
                f"ReadyForLocal — watcher will pick up on next poll.",
            )
        except Exception as exc:
            logger.warning(
                "Could not post promotion comment for %s: %s",
                manifest.ticket_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Poll and dispatch
    # ------------------------------------------------------------------

    def _dispatch_next_ticket(self) -> None:
        try:
            tickets = self._linear.list_ready_for_local()
        except Exception as exc:
            logger.warning("Linear poll failed: %s", exc)
            return

        for ticket in tickets:
            ticket_id: str = ticket["identifier"]
            all_active = self._local_active + self._cloud_active
            if any(w.ticket_id == ticket_id for w in all_active):
                continue
            labels = [
                node["name"] for node in ticket.get("labels", {}).get("nodes", [])
            ]
            if any(label.lower() == "spike" for label in labels):
                logger.warning(
                    "Skipping %s — Spike label detected; implement interactively",
                    ticket_id,
                )
                continue
            try:
                self._start_ticket(ticket_id, ticket["id"])
                return  # one ticket per dispatch cycle
            except Exception as exc:
                logger.error("Failed to start %s: %s", ticket_id, exc)

    def _start_ticket(self, ticket_id: str, linear_id: str) -> None:
        manifest = self._load_manifest(ticket_id)

        # Prerequisite checks
        open_blockers = self._linear.get_open_blockers(linear_id)
        if open_blockers:
            logger.info("Skipping %s — open blockers: %s", ticket_id, open_blockers)
            return

        all_active = self._local_active + self._cloud_active
        conflicts = check_allowed_paths_overlap(all_active, manifest)
        if conflicts:
            logger.info(
                "Deferring %s — allowed_paths overlap with active workers: %s",
                ticket_id,
                conflicts,
            )
            return

        effective_mode = resolve_effective_mode(
            self._mode, manifest.implementation_mode
        )

        if effective_mode == "local":
            if len(self._local_active) >= self._max_local_workers:
                logger.info(
                    "Deferring %s — local pool full (%d/%d)",
                    ticket_id,
                    len(self._local_active),
                    self._max_local_workers,
                )
                return
        else:
            if len(self._cloud_active) >= self._max_cloud_workers:
                logger.info(
                    "Deferring %s — cloud pool full (%d/%d)",
                    ticket_id,
                    len(self._cloud_active),
                    self._max_cloud_workers,
                )
                return

        worktree_path = create_worktree(self._repo_root, manifest)
        copy_manifest_to_worktree(self._repo_root, manifest, worktree_path)
        write_worker_pytest_config(worktree_path)

        safe_set_state(
            self._linear,
            linear_id,
            manifest.ticket_state_map.in_progress_local,
            ticket_id,
        )
        logger.info("Launching worker for %s (mode=%s)", ticket_id, effective_mode)

        if effective_mode == "local":
            self._services.ensure_ollama_running()
            self._services.ensure_litellm_running()

        backed_up_plans = backup_plan_files()
        process = launch_worker(
            self._repo_root, manifest, worktree_path, effective_mode, self._verbose
        )
        worker = ActiveWorker(
            ticket_id=ticket_id,
            linear_id=linear_id,
            manifest=manifest,
            worktree_path=worktree_path,
            process=process,
            backed_up_plans=backed_up_plans,
        )
        if effective_mode == "local":
            self._local_active.append(worker)
        else:
            self._cloud_active.append(worker)

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _reap_pool(self, workers: list[ActiveWorker]) -> list[ActiveWorker]:
        still_running: list[ActiveWorker] = []
        for worker in workers:
            rc = worker.process.poll()
            if rc is None:
                still_running.append(worker)
                continue
            elapsed = time.monotonic() - worker.start_time
            logger.info(
                "Worker %s finished (rc=%d, elapsed=%.0fs)",
                worker.ticket_id,
                rc,
                elapsed,
            )
            finalize_worker(
                worker,
                returncode=rc,
                wall_time=elapsed,
                linear=self._linear,
                metrics=self._metrics,
                escalation_policy=self._escalation_policy,
                repo_root=self._repo_root,
                mode=self._mode,
                project_id=self._project_id,
            )
            self._processed_tickets.append(
                _ProcessedTicket(
                    ticket_id=worker.ticket_id,
                    epic_id=worker.manifest.epic_id,
                    worker_branch=worker.manifest.worker_branch,
                    elapsed=elapsed,
                )
            )
        return still_running

    def _reap_finished_workers(self) -> None:
        self._local_active = self._reap_pool(self._local_active)
        self._cloud_active = self._reap_pool(self._cloud_active)

    # ------------------------------------------------------------------
    # Epic completion detection
    # ------------------------------------------------------------------

    def _has_waiting_deps(self) -> bool:
        artifacts_root = self._repo_root / _CLAUDE_DIR / "artifacts"
        if not artifacts_root.exists():
            return False
        for manifest_path in artifacts_root.glob("*/manifest.json"):
            try:
                manifest = ExecutionManifest.from_json(manifest_path)
                if manifest.status == "WaitingForDeps":
                    return True
            except Exception as exc:
                logger.warning("Could not read manifest at %s: %s", manifest_path, exc)
        return False

    def _lookup_pr_url(self, branch: str) -> str:
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
                cwd=str(self._repo_root),
                check=False,
            )
            url = result.stdout.strip()
            return url if url else "(not found)"
        except Exception:
            return "(not found)"

    def _check_epic_completion(self) -> None:
        if self._local_active or self._cloud_active:
            return
        try:
            ready = self._linear.list_ready_for_local()
        except Exception as exc:
            logger.warning("Epic completion check: Linear poll failed: %s", exc)
            return
        if ready:
            return
        if self._has_waiting_deps():
            return

        if self._processed_tickets:
            epic_id = next(
                (t.epic_id for t in self._processed_tickets if t.epic_id), None
            )
            logger.info("All sub-tickets processed — epic complete")
            logger.info("%-15s  %-55s  %s", "Ticket", "PR URL", "Elapsed")
            for t in self._processed_tickets:
                pr_url = self._lookup_pr_url(t.worker_branch)
                logger.info("%-15s  %-55s  %.0fs", t.ticket_id, pr_url, t.elapsed)
            if epic_id:
                try:
                    self._linear.post_comment(
                        epic_id,
                        f"All sub-tickets merged — ready for `/close-epic {epic_id}`",
                    )
                    logger.info("Posted epic-complete comment on %s", epic_id)
                except Exception as exc:
                    logger.warning(
                        "Could not post epic-complete comment on %s: %s", epic_id, exc
                    )

        self._running = False

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

    def _load_manifest(self, ticket_id: str) -> ExecutionManifest:
        from app.core.manifest import ArtifactPaths

        artifact = ArtifactPaths.from_ticket_id(ticket_id)
        manifest_path = self._repo_root / artifact.manifest_copy
        return ExecutionManifest.from_json(manifest_path)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def _register_signals(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: object) -> None:
        logger.info(
            "Signal %d received — finishing active workers then exiting", signum
        )
        self._services.stop()
        self._running = False

    def _wait_for_active_workers(self) -> None:
        all_active = self._local_active + self._cloud_active
        if not all_active:
            return
        logger.info("Waiting for %d active worker(s) to finish…", len(all_active))
        for worker in all_active:
            try:
                worker.process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                logger.warning("Worker %s timed out — terminating", worker.ticket_id)
                worker.process.terminate()

    # ------------------------------------------------------------------
    # PID file
    # ------------------------------------------------------------------

    def _write_pid_file(self) -> None:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pid_file(self) -> None:
        try:
            _PID_FILE.unlink()
        except FileNotFoundError:
            pass
