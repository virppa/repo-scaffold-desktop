"""Watcher / orchestrator daemon for the local worker engine.

Polls Linear for ReadyForLocal tickets, manages git worktrees, launches
claude worker sessions, collects result artifacts, runs required checks,
creates PRs, updates Linear state, and records metrics.

Usage (via CLI):
    python -m app.cli watcher [--worker-mode cloud|local]

Worker modes:
    cloud   — spawn claude with clean env (no ANTHROPIC_BASE_URL); routes to
              Anthropic API unmodified.
    local   — spawn claude --model claude-sonnet-4-6 via LiteLLM proxy on
              localhost:8082; auto-starts proxy if not already running.
    default — respect manifest.implementation_mode per ticket.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from pathlib import Path
from typing import Any, NamedTuple

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import DONE_STATE_TYPES
from app.core.manifest import ExecutionManifest
from app.core.metrics import MetricsStore

from . import dispatch
from .watcher_epic import check_epic_completion
from .watcher_finalize import finalize_worker, safe_set_state
from .watcher_heartbeat import build_tui_state, emit_heartbeat, emit_idle_line
from .watcher_log_parsing import format_elapsed, format_worker_token_count
from .watcher_services import ServiceManager
from .watcher_signals import (
    cleanup_orphaned_worktrees,
    make_signal_handler,
    maybe_warn_softstop_stuck,
    remove_pid_file,
    remove_softstop_sentinel,
    remove_stale_softstop_sentinel,
    softstop_sentinel_path,
    terminate_overrun_workers,
    wait_for_active_workers,
    write_pid_file,
)
from .watcher_tui import TrackedPR, WatcherDisplay
from .watcher_types import (
    _ARTIFACTS_DIR,
    _CLAUDE_DIR,
    ActiveWorker,
    LinearClientProtocol,
)
from .watcher_worktrees import (
    cleanup_worktree,
)

logger = logging.getLogger(__name__)

# S1192: extracted from literal used in 3 glob() calls (lines ~256, ~371, ~850)
_MANIFEST_GLOB = "*/manifest.json"

# WOR-419: multi-dispatch-per-cycle — bounds on how many eligible tickets the
# watcher dispatches in a single poll cycle. Prevents a thundering-herd of
# subprocess spawns when many tickets are queued. The loop iterates eligible
# tickets, dispatches each, then sleeps DISPATCH_DELAY_SECONDS between
# successive dispatches (gives Linear's state-change API and vLLM a breathing
# moment). Stops when the pool is full, no more eligible tickets remain, or
# MAX_DISPATCHES_PER_CYCLE is reached as a safety valve.
MAX_DISPATCHES_PER_CYCLE = 4
DISPATCH_DELAY_SECONDS = 2.5


class _ProcessedTicket(NamedTuple):
    ticket_id: str
    epic_id: str | None
    worker_branch: str
    elapsed: float
    succeeded: bool = True


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class Watcher:
    """Orchestrates local worker sessions end-to-end."""

    _POLL_INTERVAL = 10  # seconds between Linear polls

    def __init__(
        self,
        worker_mode: str = "default",
        max_local_workers: int = 8,
        max_cloud_workers: int = 3,
        linear_client: LinearClientProtocol | None = None,
        metrics_store: MetricsStore | None = None,
        repo_root: Path | None = None,
        project_id: str = "repo-scaffold-desktop",
        worker_verbose: bool = False,
        no_epic_shutdown: bool = False,
        tui_mode: bool = False,
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
        # WOR-431: dispatch.start_ticket reads services._mode for
        # effective_mode resolution. Forward the watcher's mode.
        self._services._mode = worker_mode  # type: ignore[attr-defined]
        self._worker_verbose = worker_verbose
        self._escalation_policy = EscalationPolicy.from_toml()
        self._no_epic_shutdown = no_epic_shutdown
        self._last_deferral_state: dict[str, str] = {}
        self._last_idle_state: tuple[int, int, int, bool] | None = None
        self._heartbeat: dict[str, tuple[float, int]] = {}
        self._tui_mode = tui_mode
        self._display: Any | None = None
        self._tracked_prs: list[TrackedPR] = []
        self._last_epic_complete_announced: dict[str, str] = {}
        # Soft-stop / drain mode (WOR-333). Operator writes
        # .claude/watcher.softstop to put the daemon in drain mode: stop
        # accepting new dispatches, finish in-flight workers, then exit.
        self._draining: bool = False
        self._draining_since: float | None = None
        self._softstop_warned_stuck: bool = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def _log_startup_banner(self) -> None:
        """Log the per-mode startup banner."""
        if self._mode == "cloud":
            logger.info(
                "Watcher started (mode=%s, max_cloud_workers=%d)",
                self._mode,
                self._max_cloud_workers,
            )
        elif self._mode == "local":
            logger.info(
                "Watcher started (mode=%s, max_local_workers=%d)",
                self._mode,
                self._max_local_workers,
            )
        else:
            logger.info(
                "Watcher started (mode=%s, max_local_workers=%d, max_cloud_workers=%d)",
                self._mode,
                self._max_local_workers,
                self._max_cloud_workers,
            )

    def _check_softstop_sentinel(self) -> None:
        """If the soft-stop sentinel file exists, transition into drain mode."""
        if self._draining:
            return
        sentinel = softstop_sentinel_path(self._repo_root)
        if not sentinel.exists():
            return
        self._draining = True
        self._draining_since = time.monotonic()
        active = len(self._local_active) + len(self._cloud_active)
        logger.warning(
            "Soft-stop requested. Draining: %d worker(s). "
            "Daemon exits when all finish.",
            active,
        )

    def _poll_iteration(self) -> bool:
        """One iteration of the daemon poll loop.

        Returns False if the loop should exit (drain complete or running=False).
        """
        self._check_softstop_sentinel()
        terminate_overrun_workers(self._local_active, self._cloud_active, self._linear)
        self._reap_pool(self._local_active)
        self._reap_pool(self._cloud_active)
        if self._display is not None:
            self._display.update_state(
                build_tui_state(
                    self._local_active,
                    self._cloud_active,
                    self._metrics,
                    self._tracked_prs,
                )
            )
        if not self._draining:
            self._promote_waiting_tickets()
        local_has_capacity = len(self._local_active) < self._max_local_workers
        cloud_has_capacity = len(self._cloud_active) < self._max_cloud_workers
        if not self._draining and (local_has_capacity or cloud_has_capacity):
            self._dispatch_next_ticket()
        if not self._draining:
            self._check_epic_completion()
        if self._draining and not (self._local_active or self._cloud_active):
            logger.info("Drain complete — all workers finished. Exiting.")
            remove_softstop_sentinel(self._repo_root)
            self._running = False
        return self._running

    def _emit_post_iteration_signals(self) -> None:
        """Emit idle line, heartbeat, and soft-stop warnings after a poll cycle."""
        new_idle = emit_idle_line(
            len(self._local_active),
            len(self._cloud_active),
            self._max_local_workers,
            self._max_cloud_workers,
            self._repo_root,
            self._last_idle_state,
        )
        if new_idle is not None:
            self._last_idle_state = new_idle
        self._heartbeat = emit_heartbeat(
            self._local_active,
            self._cloud_active,
            self._heartbeat,
        )
        if maybe_warn_softstop_stuck(
            self._draining,
            self._draining_since,
            self._softstop_warned_stuck,
            self._local_active,
            self._cloud_active,
        ):
            self._softstop_warned_stuck = True

    def _finalize_run(self) -> None:
        """Teardown: wait on active workers, stop services, remove pid file."""
        wait_for_active_workers(self._local_active, self._cloud_active)
        self._services.stop()
        remove_pid_file()
        if self._display is not None:
            self._display.stop()
        logger.info("Watcher stopped cleanly")

    def run(self) -> None:
        """Start the poll loop. Blocks until SIGINT/SIGTERM."""
        write_pid_file()
        handler = make_signal_handler(self._services, self)
        signal.signal(signal.SIGINT, handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handler)
        cleanup_orphaned_worktrees(self._repo_root, cleanup_worktree)
        remove_stale_softstop_sentinel(self._repo_root)
        if self._tui_mode:
            self._display = WatcherDisplay()
        if self._mode in ("local", "default"):
            self._services.probe_vllm_health()
        self._log_startup_banner()

        try:
            while self._running:
                if not self._poll_iteration():
                    break
                time.sleep(self._POLL_INTERVAL)
                self._emit_post_iteration_signals()
        finally:
            self._finalize_run()

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
        artifacts_root = self._repo_root / _CLAUDE_DIR / _ARTIFACTS_DIR
        if not artifacts_root.exists():
            return

        for manifest_path in sorted(artifacts_root.glob(_MANIFEST_GLOB)):
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
                    "All blockers for %s satisfied - promoting to ReadyForLocal",
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
            "Blocker %s for %s is %s - moving dependent to Backlog",
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
                " - manual intervention required."
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
                f"ReadyForLocal - watcher will pick up on next poll.",
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
        dispatch_count = 0
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
                    "Skipping %s - Spike label detected; implement interactively",
                    ticket_id,
                )
                continue
            try:
                self._start_ticket(ticket_id, ticket["id"])
                dispatch_count += 1
                if dispatch_count >= MAX_DISPATCHES_PER_CYCLE:
                    break
                time.sleep(DISPATCH_DELAY_SECONDS)
            except Exception as exc:
                logger.exception("Failed to start %s: %s", ticket_id, exc)

    def _start_ticket(self, ticket_id: str, linear_id: str) -> None:
        """Load + enrich the manifest, then delegate to dispatch.start_ticket.

        WOR-431: All dispatch logic (prereq checks, epic-branch gate, manifest
        quality gates, stale-epic refusal, pool/vLLM readiness, worker spawn)
        lives in dispatch.start_ticket. This wrapper handles the watcher-side
        manifest loading + retry-context enrichment that dispatch can't do.
        """
        manifest = self._load_manifest(ticket_id)
        manifest = self._enrich_with_retry_context(manifest)
        dispatch.start_ticket(
            manifest=manifest,
            linear=self._linear,
            services=self._services,
            worker_verbose=self._worker_verbose,
            _local_active=self._local_active,
            _cloud_active=self._cloud_active,
            max_cloud_workers=self._max_cloud_workers,
            _repo_root=self._repo_root,
            _processed_tickets=self._processed_tickets,  # type: ignore[arg-type]
            linear_id=linear_id,
            ticket_id=ticket_id,
            _escalation_policy=self._escalation_policy,
            _dedup_state=self._last_deferral_state,
        )

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _reap_pool(self, workers: list[ActiveWorker]) -> str:
        """Poll each worker; finalize completed ones in-place.

        Mutates ``workers`` directly - finished workers are removed from the
        list even if their ``finalize_worker`` call raises. This prevents
        ghost slots that would otherwise block future dispatch (WOR-334).

        Returns the outcome of the last finalized worker (``""`` if none
        finished, ``"failure"`` if the last one's finalize raised).
        """
        finished_indices: list[int] = []
        outcome = ""
        for i, worker in enumerate(workers):
            rc = worker.process.poll()
            if rc is None:
                continue
            # Worker finished - mark its slot for release BEFORE finalize so
            # an exception inside finalize cannot leak the slot.
            finished_indices.append(i)
            try:
                outcome = self._finalize_one_worker(worker, rc)
            except Exception as exc:
                logger.exception(
                    "finalize_worker raised for %s: %s. Worker slot freed; "
                    "result.json / last_failure.json may be incomplete and "
                    "Linear state may not have been advanced - investigate "
                    "manually.",
                    worker.ticket_id,
                    exc,
                )
                outcome = "failure"
        # Remove in reverse so earlier indices stay valid.
        for i in reversed(finished_indices):
            del workers[i]
        return outcome

    def _finalize_one_worker(self, worker: ActiveWorker, rc: int) -> str:
        """Run the per-worker finalize sequence (logging + finalize_worker call).

        Separated from ``_reap_pool`` so the latter can wrap this in try/except
        without entangling pool-management bookkeeping with finalize logic.
        """
        elapsed = time.monotonic() - worker.start_time
        # Clear heartbeat state for the finished worker
        self._heartbeat.pop(worker.ticket_id, None)
        # Final heartbeat for finished worker
        last_tick = self._heartbeat.get(worker.ticket_id, (0.0, 0))[1]
        final_tick = int(elapsed / 30)
        if final_tick > last_tick:
            elapsed_str = format_elapsed(elapsed)
            logger.info("[%s] %s", worker.ticket_id, elapsed_str)
        # Build single-line finish summary with elapsed + token count
        status = "success" if rc == 0 else "failed"
        elapsed_str = format_elapsed(elapsed)
        log_path = (
            worker.worktree_path
            / _CLAUDE_DIR
            / "logs"
            / f"{worker.ticket_id.replace('-', '_')}.jsonl"
        )
        token_str = format_worker_token_count(log_path)
        logger.info(
            "%s done (%s, %s, %s)",
            worker.ticket_id,
            status,
            elapsed_str,
            token_str,
        )
        outcome = finalize_worker(
            worker,
            returncode=rc,
            wall_time=elapsed,
            linear=self._linear,
            metrics=self._metrics,
            escalation_policy=self._escalation_policy,
            repo_root=self._repo_root,
            mode=self._mode,
            project_id=self._project_id,
            tracked_prs=self._tracked_prs,
        )
        self._processed_tickets.append(
            _ProcessedTicket(
                ticket_id=worker.ticket_id,
                epic_id=worker.manifest.epic_id,
                worker_branch=worker.manifest.worker_branch,
                elapsed=elapsed,
                succeeded=(outcome == "success"),
            )
        )
        return outcome

    # ------------------------------------------------------------------
    # Epic completion detection
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Epic completion (orchestrator wraps watcher_epic.check_epic_completion)
    # ------------------------------------------------------------------

    def _check_epic_completion(self) -> None:
        """Delegate to watcher_epic.check_epic_completion; flip _running off
        if the daemon should shut down."""
        if not check_epic_completion(
            self._local_active,
            self._cloud_active,
            self._linear,
            self._repo_root,
            self._processed_tickets,  # type: ignore[arg-type]
            self._last_epic_complete_announced,
            self._no_epic_shutdown,
        ):
            self._running = False

    def _enrich_with_retry_context(
        self, manifest: ExecutionManifest
    ) -> ExecutionManifest:
        """Prepend last_failure.json content to implementation_constraints on retry.

        Promotes the failure context from a file the worker must discover into an
        explicit directive at the top of the task list, so the worker addresses the
        specific failure immediately rather than re-running the full suite blind.
        """
        artifact_dir = (self._repo_root / manifest.artifact_paths.result_json).parent
        failure_path = artifact_dir / "last_failure.json"
        if not failure_path.exists():
            return manifest

        try:
            data = json.loads(failure_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Could not read last_failure.json for %s: %s", manifest.ticket_id, exc
            )
            return manifest

        check = data.get("check", "unknown")
        stdout = data.get("stdout", "")
        failure_line = next(
            (
                line.strip()
                for line in stdout.splitlines()
                if line.strip().startswith("FAILED")
            ),
            stdout[:200].strip(),
        )
        constraint = (
            f"RETRY: Previous run failed check `{check}`. "
            f"Fix this specific failure first: {failure_line}"
        )
        logger.info(
            "Enriching %s manifest with retry context: %s",
            manifest.ticket_id,
            failure_line,
        )
        return manifest.model_copy(
            update={
                "implementation_constraints": [
                    constraint,
                    *manifest.implementation_constraints,
                ]
            }
        )

    def _load_manifest(self, ticket_id: str) -> ExecutionManifest:
        from app.core.manifest import ArtifactPaths

        artifact = ArtifactPaths.from_ticket_id(ticket_id)
        manifest_path = self._repo_root / artifact.manifest_copy
        return ExecutionManifest.from_json(manifest_path)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
