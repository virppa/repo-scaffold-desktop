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
from typing import Any, Protocol

from app.core.manifest import ExecutionManifest
from app.core.metrics import ImplementationMode

_CLAUDE_DIR = ".claude"
_PID_FILE = Path(_CLAUDE_DIR) / "watcher.pid"
_LITELLM_PORT = 8082
_LITELLM_CONFIG = "litellm-local.yaml"
_LOCAL_MODEL = "qwen3-coder:30b"
_LITELLM_BASE_URL = f"http://localhost:{_LITELLM_PORT}"
_OLLAMA_PORT = 11434
_VLLM_PORT = 8000
_OLLAMA_KEEPALIVE = "120m"
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
    if mode in ("local", "cloud", "hybrid"):
        return mode  # type: ignore[return-value]
    return "cloud"
