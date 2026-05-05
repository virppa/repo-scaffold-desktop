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
import os
import signal
import subprocess  # nosec B404
import time
from pathlib import Path
from typing import Any, NamedTuple

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import DONE_STATE_TYPES
from app.core.manifest import ExecutionManifest
from app.core.metrics import CostRollup, MetricsStore

from .watcher_finalize import finalize_worker, safe_set_state
from .watcher_helpers import (
    check_allowed_paths_overlap,
    format_elapsed,
    format_worker_token_count,
    resolve_effective_mode,
    suppress_dedup,
)
from .watcher_services import ServiceManager
from .watcher_subprocess import launch_worker
from .watcher_tui import TrackedPR, TUIState, WatcherDisplay, WorkerState
from .watcher_types import (
    _ARTIFACTS_DIR,
    _CLAUDE_DIR,
    _PID_FILE,
    ActiveWorker,
    LinearClientProtocol,
)
from .watcher_worktrees import (
    backup_plan_files,
    cleanup_worktree,
    copy_manifest_to_worktree,
    create_worktree,
    write_worker_pytest_config,
)

logger = logging.getLogger(__name__)

# SonarCloud S1192 — shared manifest glob pattern for loading worker manifests
_MANIFEST_GLOB = "*/manifest.json"

# WOR-381: heartbeat-based stuck-worker detection. The metric is "time since
# the worker's stream-json log file was last written" — a stuck worker
# (network hang, deadlocked vLLM, infinite tool-result wait) does not emit
# new lines, while a slow-but-progressing worker keeps writing. Wall-time
# bounds proved unworkable: app.db's 33-session distribution shows median
# 43.7 min and p99 171 min for legitimate runs, so any wall-time threshold
# either fires on real work or misses real hangs. 15 minutes of log silence
# is a strong signal that the model is stuck — even multi-step tool calls
# (pytest, mypy, large diffs) emit progress within minutes.
_WORKER_HEARTBEAT_TIMEOUT_SECONDS = 15 * 60
_WORKER_KILL_GRACE_SECONDS = 5 * 60


class _ProcessedTicket(NamedTuple):
    ticket_id: str
    epic_id: str | None
    worker_branch: str
    elapsed: float
    succeeded: bool = True


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


_SOFTSTOP_SENTINEL_NAME = "watcher.softstop"
# How long the daemon may sit in drain mode before logging a stuck-worker
# warning. Keeps overnight operators from being surprised by a hung worker
# blocking graceful exit. WOR-333.
_SOFTSTOP_WARN_AFTER_MIN = 60


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
        self._worker_verbose = worker_verbose
        self._retry_counters: dict[str, int] = {}
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

    def run(self) -> None:
        """Start the poll loop. Blocks until SIGINT/SIGTERM."""
        self._write_pid_file()
        self._register_signals()
        self._cleanup_orphaned_worktrees()
        self._remove_stale_softstop_sentinel()

        if self._tui_mode:
            self._display = WatcherDisplay()

        if self._mode in ("local", "default"):
            self._services.probe_vllm_health()

        self._log_startup_info()

        try:
            while self._running:
                self._check_softstop_request()
                self._terminate_overrun_workers()
                self._reap_finished_workers()
                if self._display is not None:
                    self._display.update_state(self._build_tui_state())
                if not self._draining:
                    self._promote_waiting_tickets()
                local_has_capacity = len(self._local_active) < self._max_local_workers
                cloud_has_capacity = len(self._cloud_active) < self._max_cloud_workers
                if not self._draining and (local_has_capacity or cloud_has_capacity):
                    self._dispatch_next_ticket()
                if not self._draining:
                    self._check_epic_completion()
                if self._draining and not (self._local_active or self._cloud_active):
                    logger.info(
                        "Drain complete — all workers finished. Exiting cleanly."
                    )
                    self._remove_softstop_sentinel()
                    self._running = False
                if not self._running:
                    break
                time.sleep(self._POLL_INTERVAL)
                self._emit_idle_line()
                self._emit_heartbeat()
                self._maybe_warn_softstop_stuck()
        finally:
            self._wait_for_active_workers()
            self._services.stop()
            self._remove_pid_file()
            if self._display is not None:
                self._display.stop()
            logger.info("Watcher stopped cleanly")

    def _log_startup_info(self) -> None:
        """Log startup pool sizes, omitting irrelevant entries per mode."""
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

    def _emit_idle_line(self) -> None:
        """Emit a single idle line when the watcher has nothing active to do.

        Only re-emits when state changes (pool sizes / waiting count / capacity).
        """
        now_local = len(self._local_active)
        now_cloud = len(self._cloud_active)
        has_local = now_local < self._max_local_workers
        has_cloud = now_cloud < self._max_cloud_workers
        has_capacity = has_local or has_cloud

        # Count WaitingForDeps manifests
        artifacts_root = self._repo_root / _CLAUDE_DIR / _ARTIFACTS_DIR
        waiting = 0
        if artifacts_root.exists():
            for mp in artifacts_root.glob(_MANIFEST_GLOB):
                try:
                    m = ExecutionManifest.from_json(mp)
                except (OSError, ValueError):
                    # Missing, unreadable, or invalid manifest — skip silently;
                    # idle-line counter does not need to surface every malformed file.
                    continue
                if m.status == "WaitingForDeps":
                    waiting += 1

        state = (now_local, now_cloud, waiting, has_capacity)
        if state == self._last_idle_state:
            return
        self._last_idle_state = state

        logger.info(
            "Watcher idle — %d/%d local, %d/%d cloud, %d waiting for blockers, "
            "polling every %ds",
            now_local,
            self._max_local_workers,
            now_cloud,
            self._max_cloud_workers,
            waiting,
            self._POLL_INTERVAL,
        )

    def _emit_heartbeat(self) -> None:
        """Emit a per-worker heartbeat every ~30s with elapsed time."""
        all_active: list[ActiveWorker] = []
        all_active.extend(self._local_active)
        all_active.extend(self._cloud_active)

        for worker in all_active:
            elapsed = time.monotonic() - worker.start_time
            key = worker.ticket_id

            if key in self._heartbeat:
                _, last_tick = self._heartbeat[key]
                # Emit when we cross a new 30-second boundary
                new_tick = int(elapsed / 30)
                if new_tick <= last_tick:
                    continue
                self._heartbeat[key] = (elapsed, new_tick)
            else:
                # First emission — start at the first 30-second boundary
                tick = int(elapsed / 30)
                if tick < 1:
                    continue
                self._heartbeat[key] = (elapsed, tick)

            elapsed_str = format_elapsed(elapsed)
            logger.info("[%s] %s", worker.ticket_id, elapsed_str)

    # ------------------------------------------------------------------
    # TUI state
    # ------------------------------------------------------------------

    def _build_tui_state(self) -> TUIState:
        """Build the current TUI snapshot for the display."""
        workers: list[WorkerState] = []
        for w in self._local_active:
            elapsed = time.monotonic() - w.start_time
            workers.append(
                WorkerState(
                    ticket_id=w.ticket_id,
                    mode="local",
                    status="running",
                    elapsed_s=elapsed,
                )
            )
        for w in self._cloud_active:
            elapsed = time.monotonic() - w.start_time
            workers.append(
                WorkerState(
                    ticket_id=w.ticket_id,
                    mode="cloud",
                    status="running",
                    elapsed_s=elapsed,
                )
            )
        rollups: dict[str, CostRollup] = {}
        for period in ("today", "week", "all"):
            rollups[period] = self._metrics.get_cost_rollup(period)
        return TUIState(
            workers=workers, cost_rollups=rollups, tracked_prs=self._tracked_prs
        )

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

        Scans artifacts/ for manifests via _MANIFEST_GLOB each poll cycle.
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
        manifest = self._enrich_with_retry_context(manifest)

        # Prerequisite checks
        open_blockers = self._linear.get_open_blockers(linear_id)
        if open_blockers:
            logger.info("Skipping %s — open blockers: %s", ticket_id, open_blockers)
            return

        # Manifest-based blocker check — defense-in-depth alongside Linear check.
        for blocker_id in manifest.blocked_by_tickets:
            state_type = self._linear.get_issue_state_type(blocker_id)
            if state_type not in DONE_STATE_TYPES:
                logger.info(
                    "Skipping %s — manifest declares unmerged blocker %s (state=%s)",
                    ticket_id,
                    blocker_id,
                    state_type,
                )
                return

        all_active = self._local_active + self._cloud_active
        conflicts = check_allowed_paths_overlap(all_active, manifest)
        if conflicts:
            reason = f"overlap:{','.join(conflicts)}"
            reason_msg = (
                "Deferring %s — allowed_paths overlap with active workers: %s"
                % (ticket_id, conflicts)
            )
            if suppress_dedup(ticket_id, reason, reason_msg, self._last_deferral_state):
                logger.info(reason_msg)
            return

        effective_mode = resolve_effective_mode(
            self._mode, manifest.implementation_mode
        )

        if effective_mode == "local":
            if len(self._local_active) >= self._max_local_workers:
                reason_msg = "Deferring %s — local pool full (%d/%d)" % (
                    ticket_id,
                    len(self._local_active),
                    self._max_local_workers,
                )
                if suppress_dedup(
                    ticket_id,
                    "local_pool_full",
                    reason_msg,
                    self._last_deferral_state,
                ):
                    logger.info(reason_msg)
                return
        else:
            if len(self._cloud_active) >= self._max_cloud_workers:
                reason_msg = "Deferring %s — cloud pool full (%d/%d)" % (
                    ticket_id,
                    len(self._cloud_active),
                    self._max_cloud_workers,
                )
                if suppress_dedup(
                    ticket_id,
                    "cloud_pool_full",
                    reason_msg,
                    self._last_deferral_state,
                ):
                    logger.info(reason_msg)
                return

        if effective_mode == "local":
            if not self._services.probe_vllm_health():
                reason_msg = "Deferring %s — vLLM not ready yet" % ticket_id
                if suppress_dedup(
                    ticket_id,
                    "vllm_not_ready",
                    reason_msg,
                    self._last_deferral_state,
                ):
                    logger.warning(reason_msg)
                return
            self._services.ensure_vllm_anthropic_mode()

        worktree_path = create_worktree(self._repo_root, manifest)
        copy_manifest_to_worktree(self._repo_root, manifest, worktree_path)
        write_worker_pytest_config(worktree_path)

        safe_set_state(
            self._linear,
            linear_id,
            manifest.ticket_state_map.in_progress_local,
            ticket_id,
        )
        logger.info("Starting worker for %s — mode=%s", ticket_id, effective_mode)

        backed_up_plans = backup_plan_files()
        process = launch_worker(
            self._repo_root,
            manifest,
            worktree_path,
            effective_mode,
            self._worker_verbose,
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

    def _reap_pool(self, workers: list[ActiveWorker]) -> str:
        """Poll each worker; finalize completed ones in-place.

        Mutates ``workers`` directly — finished workers are removed from the
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
            # Worker finished — mark its slot for release BEFORE finalize so
            # an exception inside finalize cannot leak the slot.
            finished_indices.append(i)
            try:
                outcome = self._finalize_one_worker(worker, rc)
            except Exception as exc:
                logger.error(
                    "finalize_worker raised for %s: %s. Worker slot freed; "
                    "result.json / last_failure.json may be incomplete and "
                    "Linear state may not have been advanced — investigate "
                    "manually.",
                    worker.ticket_id,
                    exc,
                    exc_info=True,
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
            / ".claude"
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

    def _reap_finished_workers(self) -> None:
        self._reap_pool(self._local_active)
        self._reap_pool(self._cloud_active)

    def _terminate_overrun_workers(self) -> None:
        """Heartbeat-based stuck-worker detection (WOR-381).

        Each worker tees its stream-json log to
        ``<worktree>/.claude/worker_<ticket_lower>.log``. While the model is
        making any progress — emitting tool calls, receiving tool results,
        producing assistant text — the file's mtime advances. A genuinely
        stuck worker (vLLM unresponsive, tool subprocess hung, infinite
        deadlock) stops writing.

        Two-stage shutdown:

        1. Log idle (now − mtime) exceeds ``_WORKER_HEARTBEAT_TIMEOUT_SECONDS``
           and the process is still alive: send SIGTERM via
           ``process.terminate()``; set ``terminated_at`` to wall-clock now.
        2. ``terminated_at`` is set and the grace period has passed without
           the worker exiting: send SIGKILL via ``process.kill()`` and post a
           Linear comment with the timeout context. The natural reap path
           handles the eventual exit code.

        If the log file does not exist yet (worker just dispatched), the
        check is skipped — natural process reap handles the case where the
        worker died before writing anything.
        """
        now_wall = time.time()
        for worker in (*self._local_active, *self._cloud_active):
            log_path = (
                worker.worktree_path
                / ".claude"
                / f"worker_{worker.ticket_id.lower()}.log"
            )
            try:
                last_write = log_path.stat().st_mtime
            except (OSError, FileNotFoundError):
                # Log not yet written — let the worker warm up. Process
                # reap on later cycles handles the case where it never does.
                continue

            idle_seconds = now_wall - last_write

            if (
                worker.terminated_at is None
                and idle_seconds > _WORKER_HEARTBEAT_TIMEOUT_SECONDS
                and worker.process.poll() is None
            ):
                logger.warning(
                    "Worker %s heartbeat stalled — log idle for %.0fs "
                    "(threshold %ds). Sending SIGTERM. SIGKILL grace: %ds.",
                    worker.ticket_id,
                    idle_seconds,
                    _WORKER_HEARTBEAT_TIMEOUT_SECONDS,
                    _WORKER_KILL_GRACE_SECONDS,
                )
                try:
                    worker.process.terminate()
                except (OSError, ValueError) as exc:
                    logger.warning("Failed to SIGTERM %s: %s", worker.ticket_id, exc)
                worker.terminated_at = now_wall
                continue

            if (
                worker.terminated_at is not None
                and now_wall - worker.terminated_at > _WORKER_KILL_GRACE_SECONDS
                and worker.process.poll() is None
            ):
                logger.error(
                    "Worker %s did not exit within %ds of SIGTERM — sending SIGKILL.",
                    worker.ticket_id,
                    _WORKER_KILL_GRACE_SECONDS,
                )
                try:
                    worker.process.kill()
                except (OSError, ValueError) as exc:
                    logger.warning("Failed to SIGKILL %s: %s", worker.ticket_id, exc)
                slug = worker.ticket_id.lower().replace("-", "_")
                try:
                    self._linear.post_comment(
                        worker.linear_id,
                        (
                            f"Worker stalled: log idle {int(idle_seconds)}s "
                            f"(threshold {_WORKER_HEARTBEAT_TIMEOUT_SECONDS}s). "
                            f"SIGTERM was sent, then SIGKILL after "
                            f"{_WORKER_KILL_GRACE_SECONDS}s grace. The ticket "
                            "will be marked Blocked by the natural failure "
                            f"path. Inspect `.claude/artifacts/{slug}/` for "
                            "the partial worker log."
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not post timeout comment for %s: %s",
                        worker.ticket_id,
                        exc,
                    )

    # ------------------------------------------------------------------
    # Epic completion detection
    # ------------------------------------------------------------------

    def _has_waiting_deps(self) -> bool:
        artifacts_root = self._repo_root / _CLAUDE_DIR / _ARTIFACTS_DIR
        if not artifacts_root.exists():
            return False
        for manifest_path in artifacts_root.glob(_MANIFEST_GLOB):
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
            state_key = (
                "|".join(sorted(t.ticket_id for t in self._processed_tickets))
                + ":"
                + str(any(not t.succeeded for t in self._processed_tickets))
            )
            epic_id = next(
                (t.epic_id for t in self._processed_tickets if t.epic_id), None
            )
            if epic_id and self._last_epic_complete_announced.get(epic_id) == state_key:
                return
            if epic_id:
                self._last_epic_complete_announced[epic_id] = state_key
            failed = [t for t in self._processed_tickets if not t.succeeded]
            succeeded = [t for t in self._processed_tickets if t.succeeded]
            if failed:
                logger.warning(
                    "All tickets processed — %d failed, %d succeeded",
                    len(failed),
                    len(succeeded),
                )
            else:
                logger.info("All sub-tickets processed — epic complete")
            logger.info("%-15s  %-55s  %s", "Ticket", "PR URL", "Elapsed")
            for t in self._processed_tickets:
                pr_url = (
                    self._lookup_pr_url(t.worker_branch) if t.succeeded else "(failed)"
                )
                logger.info("%-15s  %-55s  %.0fs", t.ticket_id, pr_url, t.elapsed)
            if epic_id and not failed:
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
            if not self._no_epic_shutdown:
                self._running = False

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

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

    def _cleanup_orphaned_worktrees(self) -> None:
        from app.core.watcher.watcher_types import _WORKTREE_BASE

        base = self._repo_root.parent / _WORKTREE_BASE
        if not base.exists():
            return
        for worktree_dir in base.iterdir():
            if not worktree_dir.is_dir():
                continue
            logger.warning("Orphaned worktree detected: %s — removing", worktree_dir)
            cleanup_worktree(self._repo_root, worktree_dir)

    # ------------------------------------------------------------------
    # Soft-stop / drain mode (WOR-333)
    # ------------------------------------------------------------------

    def _softstop_sentinel_path(self) -> Path:
        return self._repo_root / _CLAUDE_DIR / _SOFTSTOP_SENTINEL_NAME

    def _remove_stale_softstop_sentinel(self) -> None:
        """Delete any sentinel left over from a prior daemon run.

        Without this, every daemon start would immediately enter drain mode
        if a previous Ctrl-C left the file behind.
        """
        sentinel = self._softstop_sentinel_path()
        if sentinel.exists():
            try:
                sentinel.unlink()
                logger.info(
                    "Removed stale soft-stop sentinel from prior run: %s", sentinel
                )
            except OSError as exc:
                logger.warning("Could not remove stale sentinel %s: %s", sentinel, exc)

    def _remove_softstop_sentinel(self) -> None:
        """Delete the sentinel during graceful drain exit."""
        sentinel = self._softstop_sentinel_path()
        try:
            sentinel.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove soft-stop sentinel %s: %s", sentinel, exc)

    def _check_softstop_request(self) -> None:
        """Detect the soft-stop sentinel and enter drain mode.

        Called once per poll cycle. Idempotent — entering drain mode is a
        one-shot transition; subsequent calls just keep `_draining` True.
        """
        if self._draining:
            return
        if not self._softstop_sentinel_path().exists():
            return
        self._draining = True
        self._draining_since = time.monotonic()
        active = len(self._local_active) + len(self._cloud_active)
        logger.warning(
            "Soft-stop requested. Draining: %d worker(s) remaining. "
            "Daemon will exit when all finish.",
            active,
        )

    def _maybe_warn_softstop_stuck(self) -> None:
        """Log a one-shot WARNING if drain has been pending too long.

        Helps the operator notice a hung worker that's blocking graceful exit.
        Threshold lives in :const:`_SOFTSTOP_WARN_AFTER_MIN`. Fires once.
        """
        if not self._draining or self._softstop_warned_stuck:
            return
        if self._draining_since is None:
            return
        elapsed_min = (time.monotonic() - self._draining_since) / 60.0
        if elapsed_min < _SOFTSTOP_WARN_AFTER_MIN:
            return
        active = self._local_active + self._cloud_active
        active_summary = ", ".join(
            f"{w.ticket_id} (running {(time.monotonic() - w.start_time) / 60:.0f}m)"
            for w in active
        )
        logger.warning(
            "Soft-stop pending for %.0f min. Worker(s) may be hung. "
            "Consider Ctrl-C to force-exit (will lose WIP). Active: %s",
            elapsed_min,
            active_summary or "(none — drain should have exited)",
        )
        self._softstop_warned_stuck = True
