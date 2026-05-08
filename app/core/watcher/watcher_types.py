"""Shared types, constants, and protocol definitions for the watcher sub-system.

This module is a leaf — it must not import from any sibling watcher module.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast, get_args

from app.core.manifest import ExecutionManifest
from app.core.metrics import ImplementationMode

_CLAUDE_DIR = ".claude"
_ARTIFACTS_DIR = "artifacts"
_PID_FILE = Path(_CLAUDE_DIR) / "watcher.pid"
_IN_PROGRESS_STATE = "In Progress"
_VLLM_PORT = 8000
# WOR-408: env-configurable vLLM endpoint. Defaults to localhost:8000 (the
# normal WSL2 → Windows port-forwarding case). Override with
# WATCHER_VLLM_BASE_URL when localhost forwarding is broken (e.g. mirrored
# networking glitches resolve only the WSL guest IP from Windows).
_VLLM_BASE_URL = os.environ.get(
    "WATCHER_VLLM_BASE_URL", f"http://localhost:{_VLLM_PORT}"
)
# Host derived from _VLLM_BASE_URL for direct http.client.HTTPConnection use
# (ServiceManager probes). Falls back to "localhost" if the URL is malformed.
_VLLM_HOST = (
    _VLLM_BASE_URL.split("://", 1)[1].partition(":")[0]
    if "://" in _VLLM_BASE_URL
    else "localhost"
) or "localhost"
_VLLM_SERVED_MODEL = "qwen3-coder"
# Metrics label kept for backward compatibility with rows written before WOR-368;
# Claude Code routes by tier via ANTHROPIC_DEFAULT_*_MODEL, so the on-the-wire
# request reaches vLLM as "qwen3-coder" but Claude Code's accounting still says
# this. A "served_model_name" column would be the cleaner long-term fix.
_LOCAL_MODEL = "claude-sonnet-4-6"
_WORKTREE_BASE = Path("worktrees")

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
    def get_issue_state_type(self, identifier: str) -> str | None: ...


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
    backed_up_plans: list[Path] = field(default_factory=list)
    retry_count: int = 0
    # WOR-363: count of OTHER active workers at the moment this one launched.
    # Captured by dispatch.start_ticket BEFORE adding self to the active pool.
    dispatch_concurrency: int = 0
    # WOR-381: wall-clock timestamp (time.time()) at which the watcher sent
    # SIGTERM after the worker's log file went stale. Used to track the
    # SIGKILL grace period. None means the worker has not been signalled.
    # Wall-clock (not monotonic) so it shares a frame of reference with the
    # log file's st_mtime.
    terminated_at: float | None = None
    # WOR-370: vLLM /metrics snapshot taken at dispatch time when the
    # worker was the only active session (dispatch_concurrency == 0).
    # None means no snapshot (either concurrency > 0 at dispatch, or the
    # /metrics endpoint was unreachable).
    vllm_metrics_before: dict[str, float] | None = None
    # WOR-370: True iff the worker was solo at dispatch AND no other worker
    # was dispatched during this session. Flipped to False whenever a peer
    # is dispatched. At reap, only workers with remained_solo=True get
    # attributable vLLM /metrics deltas; the rest get a sentinel artifact.
    remained_solo: bool = False


# ---------------------------------------------------------------------------
# Module-level utilities
# ---------------------------------------------------------------------------


def is_watcher_running(pid_file: Path = _PID_FILE) -> bool:
    """Return True if a watcher process is currently running."""
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
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
    if mode in get_args(ImplementationMode):
        return cast(ImplementationMode, mode)
    return "cloud"
