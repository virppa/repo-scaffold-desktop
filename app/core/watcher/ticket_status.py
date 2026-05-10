"""Pure functions for assembling a structured ticket-status snapshot.

Reads only: Linear issue data (via LinearClient), worker log files,
artifact directory contents, and worktree existence. No writes, no
subprocess calls.

Designed to be trivially unit-testable with mocks and tmp_path.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.manifest import ArtifactPaths

# Terminal state names that indicate a ticket is no longer in-flight.
_TERMINAL_STATES = frozenset(
    {"Done", "MergedToEpic", "Cancelled", "Duplicate", "Blocked"},
)

# Tokens in a stream-json assistant content block that indicate a tool_use
# with a Bash/Command name. We show the target (first argument) or the
# command name itself.
_TOOL_USE_NAME_RE = re.compile(r'"name"\s*:\s*"(\w+)"')
_TOOL_USE_INPUT_RE = re.compile(r'"input"\s*:\s*\{([^}]*)\}', re.DOTALL)


@dataclass
class ToolCallInfo:
    """A single tool call extracted from a worker log."""

    name: str  # e.g. "Edit", "Bash", "Read", "Grep", "Write", "Task"
    display: str  # human-friendly short description, truncated to ~40 chars


@dataclass
class LogInfo:
    """Worker log file metadata + extracted info."""

    size_bytes: int
    last_activity_ago_seconds: int | None
    last_tool_calls: list[ToolCallInfo] = field(default_factory=list)
    api_retries: int | None = None
    subagent_spawns: int | None = None


@dataclass
class ArtifactInfo:
    """Contents of the artifact directory for a ticket."""

    path: str
    entries: dict[str, int]  # filename -> byte size (int) or None if missing


@dataclass
class TicketStatus:
    """Structured snapshot of a single Linear ticket's state."""

    ticket_id: str
    title: str
    state: str
    state_age_seconds: int | None
    worker_log: LogInfo | None
    artifacts: ArtifactInfo | None
    worktree_exists: bool | None
    worktree_path: str | None
    health_flags: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """True when the ticket is in a terminal (closed) state."""
        return self.state in _TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (JSON-compatible)."""
        result: dict[str, Any] = {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "state": self.state,
            "state_age_seconds": self.state_age_seconds,
            "is_terminal": self.state in _TERMINAL_STATES,
        }
        if self.worker_log is not None:
            result["worker_log"] = self._log_to_dict()
        if self.artifacts is not None:
            result["artifacts"] = self._artifacts_to_dict()
        result["worktree_exists"] = self.worktree_exists
        if self.worktree_path is not None:
            result["worktree_path"] = self.worktree_path
        if self.health_flags:
            result["health_flags"] = self.health_flags
        return result

    def _log_to_dict(self) -> dict[str, Any]:
        log = self.worker_log
        if log is None:
            return {}
        result: dict[str, Any] = {
            "size_bytes": log.size_bytes,
            "last_activity_ago_seconds": log.last_activity_ago_seconds,
        }
        if log.last_tool_calls:
            result["last_tool_calls"] = [
                {"name": tc.name, "display": tc.display} for tc in log.last_tool_calls
            ]
        if log.api_retries is not None:
            result["api_retries"] = log.api_retries
        if log.subagent_spawns is not None:
            result["subagent_spawns"] = log.subagent_spawns
        return result

    def _artifacts_to_dict(self) -> dict[str, Any]:
        if self.artifacts is None:
            return {}
        entries: dict[str, Any] = {}
        for name, size in self.artifacts.entries.items():
            if size is None:
                entries[name] = "missing"
            else:
                entries[name] = f"{size} bytes"
        return entries


def _format_age(seconds: int | None) -> str:
    """Format seconds into a human-readable age string."""
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}m{secs:02d}s" if secs else f"{mins}m"
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def _format_size(n: int | None) -> str:
    """Format byte count to human-readable size."""
    if n is None:
        return "?"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        k = n / 1024
        return f"{k:.1f}KB" if k != int(k) else f"{int(k)}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _truncate(s: str, max_len: int = 40) -> str:
    """Truncate a string to max_len, adding ellipsis if needed."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _parse_last_tool_calls(log_path: Path) -> list[ToolCallInfo]:
    """Tail the last ~10 lines of the log and extract tool_use events.

    Returns up to 3 most recent tool calls as ToolCallInfo objects.
    Returns [] when the log is empty or has no tool_use events.
    """
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        tail = lines[-30:] if len(lines) > 30 else lines
    except Exception:
        return []

    calls: list[ToolCallInfo] = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message", {}) or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if not name:
                continue
            input_data = block.get("input") or {}
            display = _build_display(name, input_data)
            calls.append(ToolCallInfo(name=name, display=_truncate(display)))
            if len(calls) >= 3:
                return list(reversed(calls))
    return list(reversed(calls))


def _build_display(name: str, input_data: dict[str, Any]) -> str:
    """Build a short display string for a tool_use call."""
    if name == "Bash":
        cmd = input_data.get("command", "")
        if isinstance(cmd, str):
            first_token = cmd.strip().split()[0] if cmd.strip() else ""
            return f"Bash {first_token}"
        return "Bash <command>"
    if name == "Task":
        prompt = input_data.get("prompt", "")
        if isinstance(prompt, str):
            return f"Task {prompt[:40]}"
        return "Task"
    # For Edit/Read/Grep/Write/etc., show first arg if present.
    for key in ("path", "file_path", "file", "filepath"):
        val = input_data.get(key)
        if isinstance(val, str) and val:
            return f"{name} {val}"
    return name


def _parse_log_path(ticket_id_lower: str) -> Path:
    """Derive the canonical worker log path for a ticket ID."""
    slug = ticket_id_lower
    log_name = f"worker_{slug}.log"
    return Path(".claude") / "artifacts" / slug / log_name


def fetch_ticket_status(
    linear_client: Any,
    ticket_id: str,
    *,
    artifact_dir: Path | None = None,
    worktree_dir: Path | None = None,
) -> TicketStatus:
    """Assemble a structured status snapshot for *ticket_id*.

    Parameters
    ----------
    linear_client : LinearClient
        An instantiated LinearClient — only read methods are called.
    ticket_id : str
        Linear ticket identifier, e.g. "WOR-277".
    artifact_dir : Path | None
        Override artifact directory. Defaults to
        ``.claude/artifacts/<ticket_id_lower>``.
    worktree_dir : Path | None
        Override worktree directory. Defaults to
        ``.claude/worktrees/<ticket_id_slug>``.

    Returns
    -------
    TicketStatus
        A dataclass with all extracted fields.
    """
    ticket_id_lower = ticket_id.lower()

    # ---- Linear data ----
    try:
        issue = linear_client.get_issue(ticket_id)
    except Exception as exc:
        # Fallback: return minimal info when Linear is unreachable.
        return TicketStatus(
            ticket_id=ticket_id,
            title=f"(error fetching: {exc})",
            state="Unknown",
            state_age_seconds=None,
            worker_log=None,
            artifacts=None,
            worktree_exists=None,
            worktree_path=None,
        )

    title = issue.get("title", "")
    state_data = issue.get("state") or {}
    state = state_data.get("name", state_data.get("type", "Unknown"))
    state_created = state_data.get("createdAt") or state_data.get("created_at")

    state_age_seconds: int | None = None
    if state_created:
        try:
            # Linear returns ISO-8601 strings like "2026-05-10T06:19:32.000Z"
            created_dt = time.mktime(
                time.strptime(state_created[:19], "%Y-%m-%dT%H:%M:%S")
            )
            state_age_seconds = max(0, int(time.time() - created_dt))
        except (ValueError, TypeError):
            state_age_seconds = None

    # ---- Log file ----
    log_path = _parse_log_path(ticket_id_lower)
    log_info: LogInfo | None = None
    if log_path.exists():
        stat = log_path.stat()
        size_bytes = stat.st_size
        mtime = stat.st_mtime
        last_activity = max(0, int(time.time() - mtime))

        # Last 3 tool calls
        last_tool_calls = _parse_last_tool_calls(log_path)

        # API retries and subagent spawns from existing helpers
        from app.core.watcher.watcher_log_parsing import (
            _parse_worker_api_retries,
            _parse_worker_subagent_spawns,
        )

        api_retries = _parse_worker_api_retries(log_path)
        subagent_spawns = _parse_worker_subagent_spawns(log_path)

        log_info = LogInfo(
            size_bytes=size_bytes,
            last_activity_ago_seconds=last_activity,
            last_tool_calls=last_tool_calls,
            api_retries=api_retries,
            subagent_spawns=subagent_spawns,
        )

    # ---- Artifacts ----
    if artifact_dir is None:
        artifact_paths = ArtifactPaths.from_ticket_id(ticket_id)
        artifact_dir = Path(artifact_paths.result_json).parent

    entries: dict[str, int] = {}
    if artifact_dir.exists():
        for child in sorted(artifact_dir.iterdir()):
            if child.is_file():
                entries[child.name] = child.stat().st_size

    if entries:
        artifacts = ArtifactInfo(path=str(artifact_dir), entries=entries)
    else:
        artifacts = None

    # ---- Worktree ----
    worktree_path = None
    if worktree_dir is None:
        slug = ticket_id_lower.replace("-", "_")
        worktree_dir = Path(".claude") / "worktrees" / slug
    else:
        worktree_path = str(worktree_dir)

    worktree_exists = worktree_dir.exists() if worktree_dir else None
    if worktree_path is None and worktree_dir is not None:
        worktree_path = str(worktree_dir)

    # ---- Health flags ----
    health_flags: dict[str, Any] = {}
    if log_info is not None:
        if log_info.api_retries is not None:
            health_flags["api_retries"] = log_info.api_retries
        if log_info.subagent_spawns is not None:
            health_flags["subagent_spawns"] = log_info.subagent_spawns

    # If there are no artifacts beyond what's expected, flag it
    result_json_path = artifact_dir / "result.json"
    if not result_json_path.exists() and state not in (
        "Done",
        "MergedToEpic",
        "Cancelled",
        "Duplicate",
        "Blocked",
    ):
        health_flags["no_result_artifact"] = True

    return TicketStatus(
        ticket_id=ticket_id,
        title=title,
        state=state,
        state_age_seconds=state_age_seconds,
        worker_log=log_info,
        artifacts=artifacts,
        worktree_exists=worktree_exists,
        worktree_path=worktree_path,
        health_flags=health_flags,
    )
