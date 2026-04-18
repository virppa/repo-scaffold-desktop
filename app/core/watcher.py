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
import shlex
import shutil
import signal
import subprocess  # nosec B404
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol

from app.core.linear_client import LinearError
from app.core.manifest import ExecutionManifest
from app.core.metrics import ImplementationMode, MetricsStore, Outcome, TicketMetrics

logger = logging.getLogger(__name__)

_PID_FILE = Path(".claude/watcher.pid")
_LITELLM_PORT = 8082
_LITELLM_CONFIG = "litellm-local.yaml"
_LOCAL_MODEL = "qwen3-coder:30b"
_LITELLM_BASE_URL = f"http://localhost:{_LITELLM_PORT}"
_WORKTREE_BASE = Path(".claude/worktrees")

_ENV_VARS_TO_STRIP_FOR_CLOUD = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "OPENAI_API_BASE",
    }
)


# ---------------------------------------------------------------------------
# Protocol for dependency injection (testability)
# ---------------------------------------------------------------------------


class LinearClientProtocol(Protocol):
    def list_ready_for_local(self) -> list[dict[str, Any]]: ...
    def get_open_blockers(self, issue_id: str) -> list[str]: ...
    def set_state(self, issue_id: str, state_name: str) -> None: ...
    def post_comment(self, issue_id: str, body: str) -> None: ...


# ---------------------------------------------------------------------------
# Active worker tracking
# ---------------------------------------------------------------------------


@dataclass
class ActiveWorker:
    ticket_id: str
    linear_id: str
    manifest: ExecutionManifest
    worktree_path: Path
    process: subprocess.Popen[bytes]
    start_time: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Pure helper functions (unit-testable, no I/O)
# ---------------------------------------------------------------------------


def check_allowed_paths_overlap(
    active: list[ActiveWorker], candidate: ExecutionManifest
) -> list[str]:
    """Return identifiers of active workers whose allowed_paths overlap with candidate.

    Two manifests overlap when they share at least one allowed_path pattern.
    An empty allowed_paths list means "no restriction" — treated as overlap with
    everything to be safe.
    """
    if not candidate.allowed_paths:
        return [w.manifest.ticket_id for w in active]

    conflicts: list[str] = []
    candidate_set = set(candidate.allowed_paths)
    for worker in active:
        if not worker.manifest.allowed_paths or candidate_set & set(
            worker.manifest.allowed_paths
        ):
            conflicts.append(worker.manifest.ticket_id)
    return conflicts


def build_worker_env(
    mode: str,
    base_env: dict[str, str],
) -> dict[str, str]:
    """Return a subprocess environment dict for the given worker mode.

    cloud   — strips ANTHROPIC_BASE_URL and related vars so the process routes
              to the real Anthropic API.
    local   — injects ANTHROPIC_BASE_URL pointing to the LiteLLM proxy and sets
              ANTHROPIC_API_KEY=sk-dummy if not already present (LiteLLM doesn't
              validate the key; this satisfies Claude Code's auth check).
    default — passes base_env unchanged.
    """
    env = dict(base_env)
    if mode == "cloud":
        for var in _ENV_VARS_TO_STRIP_FOR_CLOUD:
            env.pop(var, None)
    elif mode == "local":
        env["ANTHROPIC_BASE_URL"] = _LITELLM_BASE_URL
        env.setdefault("ANTHROPIC_API_KEY", "sk-dummy")
    return env


def build_worker_cmd(ticket_id: str, mode: str) -> list[str]:
    """Return the claude subprocess command list for the given mode."""
    prompt = f"/implement-ticket {ticket_id}"
    # --strict-mcp-config + empty config prevents Claude Code from loading
    # .mcp.json in the worktree, which would block for ~180s trying to
    # authenticate the Linear HTTP MCP server via OAuth in non-interactive mode.
    base = [
        "claude",
        "--dangerously-skip-permissions",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    if mode == "local":
        return base + ["--model", _LOCAL_MODEL, "-p", prompt]
    return base + ["-p", prompt]


def resolve_effective_mode(worker_mode: str, manifest_mode: str) -> str:
    """Return the effective implementation mode.

    worker_mode takes precedence when it is not 'default'.
    Falls back to manifest_mode ('local', 'cloud', or 'hybrid').
    Hybrid is treated as 'cloud' for subprocess purposes.
    """
    if worker_mode != "default":
        return worker_mode
    if manifest_mode == "hybrid":
        return "cloud"
    return manifest_mode


def _tee_worker_output(
    pipe: IO[bytes],
    log_file: IO[bytes],
    prefix: bytes,
    dest: IO[bytes],
) -> None:
    """Read *pipe* line-by-line, writing each line to *log_file* and *dest*.

    Runs in a daemon thread; returns when the pipe reaches EOF (worker exit).
    Closes *log_file* in the finally block — ownership transfers from the
    caller to this thread in verbose mode.
    """
    try:
        for raw_line in pipe:
            log_file.write(raw_line)
            log_file.flush()
            dest.write(prefix + raw_line)
            dest.flush()
    finally:
        log_file.close()


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class Watcher:
    """Orchestrates local worker sessions end-to-end."""

    _POLL_INTERVAL = 30  # seconds between Linear polls

    def __init__(
        self,
        worker_mode: str = "default",
        max_workers: int = 1,
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
        self._max_workers = max_workers
        self._linear = linear_client
        self._metrics = metrics_store or MetricsStore()
        self._repo_root = (repo_root or Path.cwd()).resolve()
        self._project_id = project_id
        self._active: list[ActiveWorker] = []
        self._running = True
        self._litellm_proc: subprocess.Popen[bytes] | None = None
        self._verbose = verbose
        self._worker_counter = 0
        self._worker_counter_lock = threading.Lock()
        self._retry_counters: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the poll loop. Blocks until SIGINT/SIGTERM."""
        self._write_pid_file()
        self._register_signals()
        self._cleanup_orphaned_worktrees()

        if self._mode == "local":
            self._ensure_litellm_running()

        logger.info(
            "Watcher started (mode=%s, max_workers=%d)",
            self._mode,
            self._max_workers,
        )

        try:
            while self._running:
                self._reap_finished_workers()
                if len(self._active) < self._max_workers:
                    self._dispatch_next_ticket()
                time.sleep(self._POLL_INTERVAL)
        finally:
            self._wait_for_active_workers()
            self._remove_pid_file()
            logger.info("Watcher stopped cleanly")

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
            if any(w.ticket_id == ticket_id for w in self._active):
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

        conflicts = check_allowed_paths_overlap(self._active, manifest)
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
        worktree_path = self._create_worktree(manifest)
        self._copy_manifest_to_worktree(manifest, worktree_path)
        self._write_worker_pytest_config(worktree_path)

        self._safe_set_state(
            linear_id, manifest.ticket_state_map.in_progress_local, ticket_id
        )
        logger.info("Launching worker for %s (mode=%s)", ticket_id, effective_mode)

        process = self._launch_worker(manifest, worktree_path, effective_mode)
        self._active.append(
            ActiveWorker(
                ticket_id=ticket_id,
                linear_id=linear_id,
                manifest=manifest,
                worktree_path=worktree_path,
                process=process,
            )
        )

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    def _reap_finished_workers(self) -> None:
        still_running: list[ActiveWorker] = []
        for worker in self._active:
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
        self._active = still_running

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
        self._safe_set_state(linear_id, manifest.ticket_state_map.in_review, ticket_id)
        return "success"

    def _finalize_worker(
        self, worker: ActiveWorker, *, returncode: int, wall_time: float
    ) -> None:
        ticket_id = worker.ticket_id
        linear_id = worker.linear_id
        manifest = worker.manifest

        outcome: Outcome
        escalated = False

        if returncode != 0:
            logger.error("Worker %s exited non-zero (%d)", ticket_id, returncode)
            outcome = "failure"
            if manifest.failure_policy.escalate_to_cloud:
                logger.info("Escalating %s to cloud per failure policy", ticket_id)
                escalated = True
            self._safe_set_state(linear_id, manifest.ticket_state_map.failed, ticket_id)
        else:
            checks_ok = self._run_checks(manifest, worker.worktree_path)
            if not checks_ok and manifest.failure_policy.on_check_failure == "abort":
                outcome = "failure"
                self._safe_set_state(
                    linear_id, manifest.ticket_state_map.failed, ticket_id
                )
            else:
                outcome = self._attempt_pr(manifest, worker, ticket_id, linear_id)

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
                local_wall_time=wall_time,
                escalated_to_cloud=escalated,
                outcome=outcome,
            )
        )

        self._preserve_worker_log(worker)
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
        worktree_path = self._repo_root / _WORKTREE_BASE / worktree_name
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
        return worktree_path

    def _copy_manifest_to_worktree(
        self, manifest: ExecutionManifest, worktree_path: Path
    ) -> None:
        src = self._repo_root / manifest.artifact_paths.manifest_copy
        dest = worktree_path / manifest.artifact_paths.manifest_copy
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def _write_worker_pytest_config(self, worktree_path: Path) -> None:
        """Write pytest.ini overriding pyproject.toml addopts in the worktree.

        pytest.ini takes precedence over pyproject.toml, so this strips
        --cov-fail-under from every pytest call the worker makes. Coverage
        is still enforced by CI on the PR.
        """
        (worktree_path / "pytest.ini").write_text("[pytest]\naddopts = --tb=short\n")

    def _preserve_worker_log(self, worker: ActiveWorker) -> None:
        log_src = (
            worker.worktree_path / f".claude/worker_{worker.ticket_id.lower()}.log"
        )
        if not log_src.exists():
            return
        artifact_dir = (
            self._repo_root / worker.manifest.artifact_paths.result_json
        ).parent
        artifact_dir.mkdir(parents=True, exist_ok=True)
        dest = artifact_dir / log_src.name
        shutil.copy2(log_src, dest)
        logger.info("Worker log preserved at %s", dest)

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
        base = self._repo_root / _WORKTREE_BASE
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

    def _launch_worker(
        self,
        manifest: ExecutionManifest,
        worktree_path: Path,
        effective_mode: str,
    ) -> subprocess.Popen[bytes]:
        cmd = build_worker_cmd(manifest.ticket_id, effective_mode)
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
        subprocess.run(  # nosec B603 B607
            ["gh", "pr", "merge", "--auto", "--squash", pr_url],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=False,
        )
        return pr_url

    # ------------------------------------------------------------------
    # LiteLLM proxy
    # ------------------------------------------------------------------

    def _ensure_litellm_running(self) -> None:
        """Start the LiteLLM proxy if not already listening on _LITELLM_PORT."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            already_up = sock.connect_ex(("localhost", _LITELLM_PORT)) == 0

        if already_up:
            logger.info("LiteLLM proxy already running on port %d", _LITELLM_PORT)
            return

        config_path = self._repo_root / _LITELLM_CONFIG
        if not config_path.exists():
            raise FileNotFoundError(
                f"LiteLLM config not found: {config_path}. "
                "Copy litellm-local.yaml.example to litellm-local.yaml "
                "and configure it."
            )

        log_path = self._repo_root / ".claude" / "litellm.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "wb")  # noqa: SIM115
        logger.info(
            "Starting LiteLLM proxy (port %d)… (log: %s)", _LITELLM_PORT, log_path
        )
        env = {**os.environ, "PYTHONUTF8": "1"}
        self._litellm_proc = subprocess.Popen(  # nosec B603 B607
            [
                "litellm",
                "--config",
                str(config_path),
                "--port",
                str(_LITELLM_PORT),
                "--drop_params",
            ],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )
        self._wait_for_litellm_ready()

    def _wait_for_litellm_ready(self, timeout: float = 60.0) -> None:
        """Poll TCP until LiteLLM's port accepts connections or process dies."""
        import socket

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._litellm_proc and self._litellm_proc.poll() is not None:
                rc = self._litellm_proc.returncode
                raise RuntimeError(
                    f"LiteLLM proxy exited (rc={rc}). "
                    f"Check .claude/litellm.log for details."
                )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                if sock.connect_ex(("localhost", _LITELLM_PORT)) == 0:
                    return
            time.sleep(0.5)
        raise TimeoutError(
            f"LiteLLM proxy not ready after {timeout}s. "
            f"Check .claude/litellm.log for details."
        )

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
        self._running = False

    def _wait_for_active_workers(self) -> None:
        if not self._active:
            return
        logger.info("Waiting for %d active worker(s) to finish…", len(self._active))
        for worker in self._active:
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


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def is_watcher_running(pid_file: Path = _PID_FILE) -> bool:
    """Return True if a watcher process is currently running."""
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    # Check if process is alive (cross-platform)
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def _to_metrics_mode(mode: str) -> ImplementationMode:
    if mode in ("local", "cloud", "hybrid"):
        return mode  # type: ignore[return-value]
    return "cloud"
