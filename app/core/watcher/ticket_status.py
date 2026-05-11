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


def _parse_assistant_line(line: str) -> list[dict[str, Any]]:
    """Return tool_use blocks from a single JSONL assistant event.

    Returns [] for any line that isn't a parseable assistant event with
    a content array.
    """
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    if obj.get("type") != "assistant":
        return []
    msg = obj.get("message", {}) or {}
    content = msg.get("content") or []
    return [
        b
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")
    ]


def _parse_last_tool_calls(log_path: Path) -> list[ToolCallInfo]:
    """Tail the last ~30 lines of the log and extract the 3 most recent tool calls."""
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
        tail = lines[-30:] if len(lines) > 30 else lines
    except Exception:
        return []

    calls: list[ToolCallInfo] = []
    for line in tail:
        for block in _parse_assistant_line(line):
            display = _build_display(block["name"], block.get("input") or {})
            calls.append(ToolCallInfo(name=block["name"], display=_truncate(display)))
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

    linear_data = _fetch_linear_state(linear_client, ticket_id)
    if isinstance(linear_data, TicketStatus):
        return linear_data
    title, state, state_age_seconds = linear_data

    log_path = _parse_log_path(ticket_id_lower)
    log_info = _load_log_info(log_path) if log_path.exists() else None

    if artifact_dir is None:
        artifact_paths = ArtifactPaths.from_ticket_id(ticket_id)
        artifact_dir = Path(artifact_paths.result_json).parent
    artifacts = _load_artifacts(artifact_dir)

    worktree_exists, worktree_path = _resolve_worktree(worktree_dir, ticket_id_lower)
    health_flags = _compute_health_flags(log_info, artifact_dir, state)

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


def _fetch_linear_state(
    linear_client: Any, ticket_id: str
) -> tuple[str, str, int | None] | TicketStatus:
    """Fetch issue from Linear and extract (title, state, state_age_seconds).

    Returns a fallback TicketStatus directly when Linear is unreachable.
    """
    try:
        issue = linear_client.get_issue(ticket_id)
    except Exception as exc:
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
    if issue is None:
        return TicketStatus(
            ticket_id=ticket_id,
            title="ticket not found",
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
            created_dt = time.mktime(
                time.strptime(state_created[:19], "%Y-%m-%dT%H:%M:%S")
            )
            state_age_seconds = max(0, int(time.time() - created_dt))
        except (ValueError, TypeError):
            state_age_seconds = None
    return title, state, state_age_seconds


def _load_log_info(log_path: Path) -> LogInfo:
    """Build LogInfo from an existing worker log file."""
    from app.core.watcher.watcher_log_parsing import (
        _parse_worker_api_retries,
        _parse_worker_subagent_spawns,
    )

    stat = log_path.stat()
    return LogInfo(
        size_bytes=stat.st_size,
        last_activity_ago_seconds=max(0, int(time.time() - stat.st_mtime)),
        last_tool_calls=_parse_last_tool_calls(log_path),
        api_retries=_parse_worker_api_retries(log_path),
        subagent_spawns=_parse_worker_subagent_spawns(log_path),
    )


def _load_artifacts(artifact_dir: Path) -> ArtifactInfo | None:
    """Enumerate files in artifact_dir; None if empty or missing."""
    if not artifact_dir.exists():
        return None
    entries: dict[str, int] = {
        child.name: child.stat().st_size
        for child in sorted(artifact_dir.iterdir())
        if child.is_file()
    }
    if not entries:
        return None
    return ArtifactInfo(path=str(artifact_dir), entries=entries)


def _resolve_worktree(
    worktree_dir: Path | None, ticket_id_lower: str
) -> tuple[bool | None, str | None]:
    """Resolve worktree existence + display path."""
    if worktree_dir is None:
        slug = ticket_id_lower.replace("-", "_")
        worktree_dir = Path(".claude") / "worktrees" / slug
    return worktree_dir.exists(), str(worktree_dir)


def _compute_health_flags(
    log_info: LogInfo | None, artifact_dir: Path, state: str
) -> dict[str, Any]:
    """Aggregate health-flag dict for the snapshot."""
    health_flags: dict[str, Any] = {}
    if log_info is not None:
        if log_info.api_retries is not None:
            health_flags["api_retries"] = log_info.api_retries
        if log_info.subagent_spawns is not None:
            health_flags["subagent_spawns"] = log_info.subagent_spawns
    if not (artifact_dir / "result.json").exists() and state not in (
        "Done",
        "MergedToEpic",
        "Cancelled",
        "Duplicate",
        "Blocked",
    ):
        health_flags["no_result_artifact"] = True
    return health_flags
