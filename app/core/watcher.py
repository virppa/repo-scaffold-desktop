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

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess  # nosec B404
import sys
import threading
import time
from pathlib import Path
from typing import IO

from app.core.escalation_policy import EscalationPolicy
from app.core.linear_client import DONE_STATE_TYPES, LinearError
from app.core.manifest import ExecutionManifest
from app.core.metrics import MetricsStore, Outcome, TicketMetrics
from app.core.watcher_helpers import (
    _POLICY_FLAGS,
    _parse_worker_usage,
    _read_result_flags,
    _tee_worker_output,
    build_worker_cmd,
    build_worker_env,
    check_allowed_paths_overlap,
    resolve_effective_mode,
)
from app.core.watcher_helpers import (
    _parse_ollama_model as _parse_ollama_model,  # noqa: F401 — backward-compat re-export
)
from app.core.watcher_services import ServiceManager
from app.core.watcher_types import (
    _CLAUDE_DIR,
    _LOCAL_MODEL,
    _PID_FILE,
    _WORKTREE_BASE,
    ActiveWorker,
    _to_metrics_mode,
)
from app.core.watcher_types import (
    LinearClientProtocol as LinearClientProtocol,  # noqa: F401 — backward-compat re-export
)
from app.core.watcher_types import (
    is_watcher_running as is_watcher_running,  # noqa: F401 — backward-compat re-export
)

logger = logging.getLogger(__name__)


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
        self._running = True
        self._services = ServiceManager(self._repo_root)
        self._verbose = verbose
        self._worker_counter = 0
        self._worker_counter_lock = threading.Lock()
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
                time.sleep(self._POLL_INTERVAL)
        finally:
            self._wait_for_active_workers()
            self._services.stop()
            self._remove_pid_file()
            logger.info("Watcher stopped cleanly")

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

            cancelled = self._find_cancelled_blocker(manifest)
            if cancelled is not None:
                blocker_id, state_type = cancelled
                self._handle_cancelled_predecessor(
                    manifest, manifest_path, blocker_id, state_type
                )
                continue

            if self._all_blockers_satisfied(manifest):
                logger.info(
                    "All blockers for %s satisfied — promoting to ReadyForLocal",
                    manifest.ticket_id,
                )
                self._transition_waiting_manifest(
                    manifest, manifest_path, "ReadyForLocal"
                )
                self._notify_promotion(manifest)

    def _find_cancelled_blocker(
        self, manifest: ExecutionManifest
    ) -> tuple[str, str] | None:
        """Return (blocker_id, state_type) for the first cancelled blocker, or None."""
        for blocker_id in manifest.blocked_by_tickets:
            try:
                state_type = self._linear.get_issue_state_type(blocker_id)
            except Exception as exc:
                logger.debug(
                    "Could not fetch state for blocker %s while scanning for "
                    "cancellations in %s: %s",
                    blocker_id,
                    manifest.ticket_id,
                    exc,
                )
                continue
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
        self._safe_set_state(manifest.linear_id, "Backlog", manifest.ticket_id)
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

    def _all_blockers_satisfied(self, manifest: ExecutionManifest) -> bool:
        for blocker_id in manifest.blocked_by_tickets:
            try:
                state_type = self._linear.get_issue_state_type(blocker_id)
            except Exception as exc:
                logger.warning(
                    "Could not fetch state for blocker %s of %s: %s",
                    blocker_id,
                    manifest.ticket_id,
                    exc,
                )
                return False
            if state_type is None or state_type not in DONE_STATE_TYPES:
                return False
            if state_type == "cancelled":
                return False
        return True

    def _notify_promotion(self, manifest: ExecutionManifest) -> None:
        if not manifest.linear_id:
            return
        self._safe_set_state(manifest.linear_id, "ReadyForLocal", manifest.ticket_id)
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

        worktree_path = self._create_worktree(manifest)
        self._copy_manifest_to_worktree(manifest, worktree_path)
        self._write_worker_pytest_config(worktree_path)

        self._safe_set_state(
            linear_id, manifest.ticket_state_map.in_progress_local, ticket_id
        )
        logger.info("Launching worker for %s (mode=%s)", ticket_id, effective_mode)

        if effective_mode == "local":
            self._ensure_ollama_running()
            self._ensure_litellm_running()

        backed_up_plans = self._backup_plan_files()
        process = self._launch_worker(manifest, worktree_path, effective_mode)
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
            self._finalize_worker(worker, returncode=rc, wall_time=elapsed)
        return still_running

    def _reap_finished_workers(self) -> None:
        self._local_active = self._reap_pool(self._local_active)
        self._cloud_active = self._reap_pool(self._cloud_active)

    def _safe_set_state(self, linear_id: str, state: str, ticket_id: str) -> None:
        try:
            self._linear.set_state(linear_id, state)
        except LinearError as exc:
            logger.warning(
                "set_state failed for %s (state=%s): %s", ticket_id, state, exc
            )

    def _attempt_pr(
        self,
        manifest: ExecutionManifest,
        worker: ActiveWorker,
        ticket_id: str,
        linear_id: str,
    ) -> Outcome:
        try:
            pr_url = self._create_pr(manifest, worker.worktree_path)
        except subprocess.CalledProcessError as exc:
            err_detail = (exc.stderr or exc.stdout or str(exc)).strip()
            logger.error("PR creation failed for %s: %s", ticket_id, err_detail)
            self._safe_set_state(linear_id, manifest.ticket_state_map.failed, ticket_id)
            try:
                body = f"PR creation failed for `{ticket_id}`:\n```\n{err_detail}\n```"
                self._linear.post_comment(linear_id, body)
            except Exception:
                logger.warning(
                    "Could not post failure comment to Linear for %s", ticket_id
                )
            return "failure"
        logger.info("PR created for %s: %s", ticket_id, pr_url)
        return "success"

    def _finalize_worker(
        self, worker: ActiveWorker, *, returncode: int, wall_time: float
    ) -> None:
        ticket_id = worker.ticket_id
        linear_id = worker.linear_id
        manifest = worker.manifest

        outcome: Outcome
        escalated = False
        artifacts_preserved = False
        sonar_findings: list[str] | None = None

        if returncode != 0:
            logger.error("Worker %s exited non-zero (%d)", ticket_id, returncode)
            outcome = "failure"
            if manifest.failure_policy.escalate_to_cloud:
                logger.info("Escalating %s to cloud per failure policy", ticket_id)
                escalated = True
            self._safe_set_state(linear_id, manifest.ticket_state_map.failed, ticket_id)
        else:
            checks_ok = self._run_checks(manifest, worker.worktree_path)
            if not checks_ok:
                worker.retry_count += 1
            if not checks_ok and manifest.failure_policy.on_check_failure == "abort":
                outcome = "failure"
                self._safe_set_state(
                    linear_id, manifest.ticket_state_map.failed, ticket_id
                )
            else:
                self._preserve_worker_artifacts(worker)
                artifacts_preserved = True

                result_path = self._repo_root / manifest.artifact_paths.result_json
                flags = _read_result_flags(result_path)
                action = self._escalation_policy.classify_result(**flags)

                if action == "escalate":
                    triggering = next(
                        (f for f in _POLICY_FLAGS if flags.get(f)), "unknown"
                    )
                    logger.info(
                        "Escalating %s to cloud (flag=%s)", ticket_id, triggering
                    )
                    escalated = True
                    self._safe_set_state(linear_id, "In Progress", ticket_id)
                    try:
                        self._linear.post_comment(
                            linear_id,
                            f"Local worker escalating `{ticket_id}` to cloud. "
                            f"Triggering flag: `{triggering}`.",
                        )
                    except Exception:
                        logger.warning(
                            "Could not post escalation comment for %s", ticket_id
                        )
                    outcome = "escalated"
                elif action == "human":
                    logger.info("Human review required for %s per policy", ticket_id)
                    try:
                        self._linear.post_comment(
                            linear_id,
                            f"Human review required for `{ticket_id}` before "
                            f"proceeding. Please inspect the result artifact.",
                        )
                    except Exception:
                        logger.warning(
                            "Could not post human review comment for %s", ticket_id
                        )
                    outcome = "aborted"
                else:  # fix_locally — classify Sonar severities before creating PR
                    sonar_findings = self._fetch_sonar_findings(manifest.worker_branch)
                    sonar_escalate = False
                    if sonar_findings:
                        for severity in sonar_findings:
                            sonar_action = (
                                self._escalation_policy.classify_sonar_finding(
                                    severity.lower()
                                )
                            )
                            if sonar_action == "escalate":
                                sonar_escalate = True
                            else:
                                logger.warning(
                                    "Sonar finding for %s: severity=%s — fix_locally",
                                    ticket_id,
                                    severity,
                                )
                    if sonar_escalate:
                        escalated = True
                        self._safe_set_state(linear_id, "In Progress", ticket_id)
                        try:
                            self._linear.post_comment(
                                linear_id,
                                f"Local worker escalating `{ticket_id}` to cloud due "
                                f"to Sonar finding requiring immediate action.",
                            )
                        except Exception:
                            logger.warning(
                                "Could not post Sonar escalation comment for %s",
                                ticket_id,
                            )
                        outcome = "escalated"
                    else:
                        outcome = self._attempt_pr(
                            manifest, worker, ticket_id, linear_id
                        )

        log_path = (
            worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
        )
        local_tokens, context_compactions = _parse_worker_usage(log_path)
        eff = resolve_effective_mode(self._mode, manifest.implementation_mode)
        self._metrics.record(
            TicketMetrics(
                ticket_id=ticket_id,
                project_id=self._project_id,
                epic_id=manifest.epic_id,
                implementation_mode=_to_metrics_mode(eff),
                local_used=(eff == "local"),
                local_model=(_LOCAL_MODEL if eff == "local" else None),
                cloud_used=(eff == "cloud"),
                local_tokens=local_tokens,
                local_wall_time=wall_time,
                escalated_to_cloud=escalated,
                outcome=outcome,
                retry_count=worker.retry_count,
                context_compactions=context_compactions,
                sonar_findings_count=(
                    len(sonar_findings) if sonar_findings is not None else None
                ),
            )
        )

        self._restore_plan_files(worker.backed_up_plans)
        if not artifacts_preserved:
            self._preserve_worker_artifacts(worker)
        self._cleanup_worktree(worker.worktree_path)

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

    def _load_manifest(self, ticket_id: str) -> ExecutionManifest:
        from app.core.manifest import ArtifactPaths

        artifact = ArtifactPaths.from_ticket_id(ticket_id)
        manifest_path = self._repo_root / artifact.manifest_copy
        return ExecutionManifest.from_json(manifest_path)

    # ------------------------------------------------------------------
    # Worktree management
    # ------------------------------------------------------------------

    def _create_worktree(self, manifest: ExecutionManifest) -> Path:
        worktree_name = manifest.worktree_name or manifest.worker_branch
        if ".." in Path(worktree_name).parts:
            raise ValueError(f"Invalid worktree name: {worktree_name!r}")
        worktree_path = self._repo_root.parent / _WORKTREE_BASE / worktree_name
        subprocess.run(  # nosec B603 B607
            [
                "git",
                "-C",
                str(self._repo_root),
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
        self._rebase_worktree_from_base(worktree_path, manifest.base_branch)
        return worktree_path

    def _rebase_worktree_from_base(self, worktree_path: Path, base_branch: str) -> None:
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

    def _copy_manifest_to_worktree(
        self, manifest: ExecutionManifest, worktree_path: Path
    ) -> None:
        src = self._repo_root / manifest.artifact_paths.manifest_copy
        dest = worktree_path / manifest.artifact_paths.manifest_copy
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def _backup_plan_files(self) -> list[Path]:
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

    def _restore_plan_files(self, backed_up: list[Path]) -> None:
        """Restore plan files moved by _backup_plan_files."""
        if not backed_up:
            return
        plans_dir = Path.home() / _CLAUDE_DIR / "plans"
        plans_dir.mkdir(exist_ok=True)
        for plan_file in backed_up:
            shutil.move(str(plan_file), plans_dir / plan_file.name)
        logger.debug("Restored %d plan file(s)", len(backed_up))

    def _write_worker_pytest_config(self, worktree_path: Path) -> None:
        """Write pytest.ini overriding pyproject.toml addopts in the worktree.

        pytest.ini takes precedence over pyproject.toml, so this strips
        --cov-fail-under from every pytest call the worker makes. Coverage
        is still enforced by CI on the PR.
        """
        (worktree_path / "pytest.ini").write_text("[pytest]\naddopts = --tb=short\n")

    def _preserve_worker_artifacts(self, worker: ActiveWorker) -> None:
        """Copy worker log and result.json from the worktree to the repo artifact dir.

        The worktree is removed after this call, so any file not copied here is lost.
        """
        artifact_dir = (
            self._repo_root / worker.manifest.artifact_paths.result_json
        ).parent
        artifact_dir.mkdir(parents=True, exist_ok=True)

        log_src = (
            worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
        )
        if log_src.exists():
            shutil.copy2(log_src, artifact_dir / log_src.name)
            logger.info("Worker log preserved at %s", artifact_dir / log_src.name)

        result_src = worker.worktree_path / worker.manifest.artifact_paths.result_json
        if result_src.exists():
            shutil.copy2(result_src, artifact_dir / result_src.name)
            logger.info(
                "Result artifact preserved at %s", artifact_dir / result_src.name
            )
        else:
            logger.warning(
                "No result artifact found at %s for %s",
                result_src,
                worker.ticket_id,
            )

    def _cleanup_worktree(self, worktree_path: Path) -> None:
        try:
            subprocess.run(  # nosec B603 B607
                [
                    "git",
                    "-C",
                    str(self._repo_root),
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
            logger.warning(
                "Failed to remove worktree %s: %s", worktree_path, exc.stderr
            )

    def _cleanup_orphaned_worktrees(self) -> None:
        """Remove any leftover watcher-managed worktrees from a prior run."""
        base = self._repo_root.parent / _WORKTREE_BASE
        if not base.exists():
            return
        for worktree_dir in base.iterdir():
            if not worktree_dir.is_dir():
                continue
            logger.warning("Orphaned worktree detected: %s — removing", worktree_dir)
            self._cleanup_worktree(worktree_dir)

    # ------------------------------------------------------------------
    # Worker subprocess
    # ------------------------------------------------------------------

    def _expand_skill(self, ticket_id: str) -> str | None:
        """Return the implement-ticket skill content with $ARGUMENTS substituted.

        Returns None if the skill file cannot be read (caller falls back to
        the /implement-ticket shortcut).
        """
        skill_path = self._repo_root / _CLAUDE_DIR / "commands" / "implement-ticket.md"
        try:
            return skill_path.read_text(encoding="utf-8").replace(
                "$ARGUMENTS", ticket_id
            )
        except OSError:
            logger.warning("Could not read skill file %s; using shortcut", skill_path)
            return None

    @staticmethod
    def _build_snippet_tool_restrictions(snippets: list[str]) -> list[str]:
        """Return --disallowed-tools patterns derived from context_snippets headers.

        Each snippet starts with a comment line like:
            # app/core/watcher.py lines 574-589
        We extract the basename and return glob patterns that block Read on those
        files regardless of the absolute path the worker uses.
        """
        import re

        seen: set[str] = set()
        patterns: list[str] = []
        header_re = re.compile(r"^#\s+(\S+)\s+lines?\s+\d")
        for snippet in snippets:
            first_line = snippet.splitlines()[0] if snippet else ""
            m = header_re.match(first_line)
            if m:
                basename = Path(m.group(1)).name
                if basename not in seen:
                    seen.add(basename)
                    patterns.append(f"Read(*{basename})")
        return patterns

    def _launch_worker(
        self,
        manifest: ExecutionManifest,
        worktree_path: Path,
        effective_mode: str,
    ) -> subprocess.Popen[bytes]:
        prompt = self._expand_skill(manifest.ticket_id)

        disallowed_tools: list[str] | None = None
        if manifest.context_snippets and effective_mode == "cloud":
            disallowed_tools = self._build_snippet_tool_restrictions(
                manifest.context_snippets
            )
            if disallowed_tools and prompt:
                file_list = ", ".join(
                    p.removeprefix("Read(*").removesuffix(")") for p in disallowed_tools
                )
                warning = (
                    f"CRITICAL: The following files are pre-loaded as context_snippets "
                    f"in the manifest: {file_list}. "
                    f"DO NOT use the Read tool on these files — "
                    f"the tool is blocked and attempting to read them will "
                    f"abort the task. "
                    f"Use only the snippets already provided.\n\n"
                )
                prompt = warning + (prompt or "")

        cmd = build_worker_cmd(
            manifest.ticket_id, effective_mode, worktree_path, prompt, disallowed_tools
        )
        env = build_worker_env(effective_mode, dict(os.environ))

        log_path = worktree_path / f".claude/worker_{manifest.ticket_id.lower()}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "wb")  # noqa: SIM115

        if self._verbose:
            with self._worker_counter_lock:
                self._worker_counter += 1
            prefix = f"[{manifest.ticket_id}] ".encode()
            process = subprocess.Popen(  # nosec B603 B607
                cmd,
                cwd=str(worktree_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None  # guaranteed by stdout=PIPE  # nosec B101
            stderr_buf: IO[bytes] = (
                getattr(sys.stderr, "buffer", None) or sys.stderr.buffer
            )
            threading.Thread(
                target=_tee_worker_output,
                args=(process.stdout, log_file, prefix, stderr_buf),
                daemon=True,
                name=f"tee-{manifest.ticket_id}",
            ).start()
            return process

        return subprocess.Popen(  # nosec B603 B607
            cmd,
            cwd=str(worktree_path),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    # ------------------------------------------------------------------
    # Check runner
    # ------------------------------------------------------------------

    def _run_checks(self, manifest: ExecutionManifest, worktree_path: Path) -> bool:
        all_passed = True
        for check_cmd in manifest.required_checks:
            logger.info("Running check: %s", check_cmd)
            result = subprocess.run(  # nosec B603
                shlex.split(check_cmd),
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(
                    "Check failed: %s\n%s", check_cmd, result.stdout + result.stderr
                )
                all_passed = False
        return all_passed

    # ------------------------------------------------------------------
    # SonarCloud findings count (Option B: REST API, best-effort)
    # ------------------------------------------------------------------

    def _fetch_sonar_findings(self, branch: str) -> list[str] | None:
        # Calls the SonarCloud issues API for the worker branch to get per-severity
        # issue data for escalation classification.  Returns a list of severity
        # strings (e.g. ['BLOCKER', 'CRITICAL']) or None when SONAR_TOKEN /
        # SONAR_PROJECT_KEY are absent or the API call fails.  An empty list means
        # the branch was scanned and has no open issues.
        import base64
        import ssl
        import urllib.parse
        import urllib.request

        token = os.environ.get("SONAR_TOKEN")
        project_key = os.environ.get("SONAR_PROJECT_KEY")
        if not token or not project_key:
            return None

        params = urllib.parse.urlencode(
            {
                "componentKeys": project_key,
                "branch": branch,
                "resolved": "false",
                "ps": "500",
            }
        )
        url = f"https://sonarcloud.io/api/issues/search?{params}"
        creds = base64.b64encode(f"{token}:".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(  # nosec B310  # nosemgrep
                req, timeout=10, context=ctx
            ) as resp:
                data: dict[str, object] = json.loads(resp.read())
            issues = data.get("issues") or []
            return [
                str(issue["severity"])
                for issue in (issues if isinstance(issues, list) else [])
                if isinstance(issue, dict) and issue.get("severity")
            ]
        except Exception:
            logger.debug(
                "Could not fetch Sonar findings for branch %s", branch, exc_info=True
            )
        return None

    # ------------------------------------------------------------------
    # PR creation
    # ------------------------------------------------------------------

    def _create_pr(self, manifest: ExecutionManifest, worktree_path: Path) -> str:
        subprocess.run(  # nosec B603 B607
            ["git", "push", "-u", "origin", manifest.worker_branch],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
        ahead = subprocess.run(  # nosec B603 B607
            [
                "git",
                "log",
                f"origin/{manifest.base_branch}..{manifest.worker_branch}",
                "--oneline",
            ],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if not ahead.stdout.strip():
            raise subprocess.CalledProcessError(
                1,
                "git log",
                stderr=(
                    f"No commits on {manifest.worker_branch} ahead of "
                    f"{manifest.base_branch} — worker did not commit any changes"
                ),
            )
        result = subprocess.run(  # nosec B603 B607
            [
                "gh",
                "pr",
                "create",
                "--base",
                manifest.base_branch,
                "--head",
                manifest.worker_branch,
                "--title",
                f"{manifest.ticket_id} {manifest.title}",
                "--body",
                f"Closes {manifest.ticket_id}\n\n{manifest.done_definition}",
            ],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        pr_url = result.stdout.strip()
        merge_result = subprocess.run(  # nosec B603 B607
            ["gh", "pr", "merge", "--auto", "--squash", pr_url],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if merge_result.returncode != 0:
            output = (merge_result.stderr or merge_result.stdout).strip()
            # "clean status" means no required checks on the target branch (e.g. epic
            # branches) — PR is already mergeable, so fall back to immediate merge.
            if "enablePullRequestAutoMerge" in output or "clean status" in output:
                logger.info(
                    "No required checks on target branch — merging %s immediately",
                    pr_url,
                )
                immediate = subprocess.run(  # nosec B603 B607
                    ["gh", "pr", "merge", "--squash", pr_url],
                    cwd=str(worktree_path),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if immediate.returncode != 0:
                    imm_output = (immediate.stderr or immediate.stdout).strip()
                    logger.warning(
                        "gh pr merge --squash also failed for %s (rc=%d): %s",
                        pr_url,
                        immediate.returncode,
                        imm_output,
                    )
            else:
                logger.warning(
                    "gh pr merge --auto failed for %s (rc=%d): %s",
                    pr_url,
                    merge_result.returncode,
                    output,
                )
        return pr_url

    # ------------------------------------------------------------------
    # Service shims — delegate to self._services; removed in test-split step
    # ------------------------------------------------------------------

    def _ensure_ollama_running(self) -> None:
        self._services.ensure_ollama_running()

    def _ensure_litellm_running(self) -> None:
        self._services.ensure_litellm_running()

    def _stop_litellm_proxy(self) -> None:
        self._services.stop()

    @property
    def _litellm_proc(self) -> subprocess.Popen[bytes] | None:
        return self._services._litellm_proc

    @_litellm_proc.setter
    def _litellm_proc(self, value: subprocess.Popen[bytes] | None) -> None:
        self._services._litellm_proc = value

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
