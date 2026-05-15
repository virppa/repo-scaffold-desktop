"""PostToolUse hook: parallel-tool nudge for independent single-tool turns.

Detects consecutive independent single-tool turns within a short time window
and emits a nudge to emit them as parallel tool_use blocks next turn.

State file: ``<repo>/.claude/.parallel_nudge_state.json``. Keyed by
``session_id`` so a stale state from a prior session does not leak into the
current one. Persisted before emitting the nudge so the counter is durable
across concurrent PostToolUse calls (parallel tool calls from the same turn
race to read/write).

Wire in ``.claude/settings.json``::

    "hooks": {
      "PostToolUse": [
        {
          "matcher": "Read|Bash|Edit|Write",
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/posttooluse_parallel_nudge.py"
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Common file extensions for path detection in tool inputs.
_EXT_RE = (
    r"(?:py|txt|md|json|yml|yaml|"
    r"toml|cfg|ini|sh|bash|env|"
    r"csv|ts|tsx|jsx|js|css|html)"
)

CONSECUTIVE_THRESHOLD = 3
TIMEOUT_SECONDS = 300  # 5 min — reset after a long idle period


def _is_tool_output_str(tool_output: object) -> str:
    """Return the tool output as a string, empty string on None/other."""
    if isinstance(tool_output, str):
        return tool_output
    return ""


_FILE_MATCH = (
    rf"[/\\][\w./\\-]+\.({_EXT_RE})"
    r"(?:\s|$|[^a-zA-Z0-9])"
)

_DIRMATCH_END = rf"[/\\][\w./\\-]+\.({_EXT_RE})$"


def _extract_file_paths_from_input(tool_input: object) -> list[str]:
    """Collect file paths that appear anywhere in the tool input."""
    paths: list[str] = []
    if not isinstance(tool_input, dict):
        return paths
    for value in tool_input.values():
        if isinstance(value, str):
            _add_paths_from_str(value, paths)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    _add_paths_from_str(item, paths)
    return paths


def _add_paths_from_str(text: str, paths: list[str]) -> None:
    """Extract file paths from a string value."""
    # Exact match (for file_path fields)
    for m in re.finditer(_DIRMATCH_END, text):
        p = m.group(0).rstrip()
        paths.append(p)
    # Partial match (for general text)
    for m in re.finditer(_FILE_MATCH, text):
        p = m.group(0).rstrip()
        if p not in paths:
            paths.append(p)


def _track_files_for_write(
    tool_name: str,
    tool_input: object,
    output_text: str,
    seen_files: dict[str, list[str]],
) -> None:
    """Track files that a Bash call creates (write-targets)."""
    if tool_name != "Bash":
        return
    if not isinstance(tool_input, dict):
        return
    cmd = tool_input.get("command", "")
    if not isinstance(cmd, str):
        return
    write_paths: list[str] = []
    write_paths.extend(re.findall(r">>\s*(\S+)", cmd))
    write_paths.extend(re.findall(r">\s*(\S+)", cmd))
    write_paths.extend(re.findall(r"tee\s+([\w\-/.]+)", cmd))
    cp_mv = re.findall(r"(?:cp|mv)\s+.*?([\w\-/.]+)$", cmd)
    write_paths.extend(cp_mv)
    write_paths.extend(re.findall(r"(\S+)(?:\s*>)", cmd))

    valid_paths: list[str] = []
    for p in write_paths:
        p = p.strip("\"'")
        if not p or p.startswith("|") or p.startswith("&"):
            continue
        if re.search(
            r"\.(py|txt|md|json|yml|"
            r"yaml|cfg|ini|sh|bash|"
            r"env|csv|log)$",
            p,
        ):
            valid_paths.append(p)

    if valid_paths:
        for p in valid_paths:
            candidate = Path(p)
            if not candidate.is_absolute():
                candidate = Path.cwd() / p
            if candidate.exists():
                if str(candidate) not in seen_files:
                    seen_files[str(candidate)] = []
                for vp in valid_paths:
                    cp = Path(vp)
                    if not cp.is_absolute():
                        cp = Path.cwd() / vp
                    cp_str = str(cp)
                    if cp_str not in seen_files[str(candidate)]:
                        seen_files[str(candidate)].append(cp_str)


def _has_dependency(
    earlier_name: str,
    earlier_input: object,
    earlier_output: str,
    earlier_files: list[str],
    later_name: str,
    later_input: object,
    later_output: str,
    later_files: list[str],
) -> bool:
    """Return True if the later tool depends on the earlier tool.

    A dependency exists when:
    - Later input or output references files the earlier tool produced.
    - The file actually exists on disk (the hook fires post-tool).
    """
    earlier_set = set(earlier_files)
    later_set = set(later_files)
    common = earlier_set & later_set
    for path in common:
        candidate = Path(path)
        if candidate.is_absolute() and candidate.exists():
            return True
        if not candidate.is_absolute() and candidate.exists():
            return True

    # Same-file detection via basename — the same file may be extracted as
    # different path strings (e.g. "src/result.txt" vs the full path).
    earlier_basenames = {Path(p).name: p for p in earlier_files}
    for lp in later_files:
        lp_path = Path(lp)
        for eb_name, ep in earlier_basenames.items():
            if lp_path.name != eb_name:
                continue
            ep_path = Path(ep)
            if ep_path.exists():
                return True
            # Suffix match: earlier relative path is a suffix of later path.
            if lp.endswith(ep) or lp.endswith(ep.replace("/", "\\")):
                return True
    return False


def _nudge_maybe(state: dict) -> dict | None:
    """Check if consecutive independent turns reached threshold."""
    history = state.get("history", [])
    if len(history) < CONSECUTIVE_THRESHOLD:
        return None

    window = history[-CONSECUTIVE_THRESHOLD:]

    for i in range(len(window)):
        earlier = window[i]
        for j in range(i + 1, len(window)):
            later = window[j]
            if _has_dependency(
                earlier["name"],
                earlier["input"],
                earlier.get("output", ""),
                earlier.get("files", []),
                later["name"],
                later["input"],
                later.get("output", ""),
                later.get("files", []),
            ):
                return None

    tools_in_window = [t["name"] for t in window]
    return {
        "decision": "nudge",
        "reason": (
            "Consecutive independent single-tool "
            "turns detected. Emit independent tool "
            "calls in parallel to reduce "
            "turn-boundary overhead."
        ),
        "consecutive_count": len(window),
        "tools": tools_in_window,
    }


def _format_nudge(nudge: dict) -> str:
    """Format nudge as a readable message."""
    tools = nudge.get("tools", [])
    count = nudge.get("consecutive_count", 0)
    parts = [
        "[parallel-tool-nudge] Detected",
        f"{count} consecutive independent",
        "single-tool turns",
        f"({', '.join(tools)})",
        "Per CLAUDE.md: emit independent",
        "tool calls in parallel —",
        "consider batching independent",
        "calls in one message to avoid",
        f"{count}x turn-boundary",
        "warmup. Each turn boundary",
        "costs full prefill + decode",
        "warmup (10-30s on long context).",
    ]
    return " ".join(parts)


def _state_path() -> Path:
    """Return the state file path anchored to the hook's location."""
    override = os.environ.get("PARALLEL_NUDGE_STATE_DIR")
    if override:
        return Path(override) / ".parallel_nudge_state.json"
    hook_dir = Path(__file__).resolve().parent
    return hook_dir.parent / ".parallel_nudge_state.json"


def _load_state(path: Path) -> dict:
    """Load session state. Reset if session_id mismatch."""
    state: dict = {
        "session_id": "",
        "window_start": 0.0,
        "counter": 0,
        "history": [],
        "last_nudge": 0.0,
    }
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state.update(loaded)
        except (json.JSONDecodeError, OSError):
            pass
    return state


def _save_state(path: Path, state: dict) -> None:
    """Persist session state to disk."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    print(
        "posttooluse_parallel_nudge: fired",
        file=sys.stderr,
        flush=True,
    )

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_output_raw = payload.get("tool_output", "")
    session_id = payload.get("session_id", "")
    message_id = payload.get("message_id", "")

    output_text = _is_tool_output_str(tool_output_raw)

    # Allow state dir override via payload (for test isolation)
    payload_state = payload.get("state_dir", "")
    if payload_state:
        state_path = Path(payload_state) / ".parallel_nudge_state.json"
    else:
        state_path = _state_path()

    state = _load_state(state_path)

    if state.get("session_id") != session_id:
        state["session_id"] = session_id
        state["window_start"] = time.time()
        state["counter"] = 0
        state["history"] = []
        state["last_nudge"] = 0.0

    timestamp = float(payload.get("timestamp", time.time()))

    tool_files = _extract_file_paths_from_input(tool_input)
    print(f"DEBUG {tool_name} files={tool_files}", file=sys.stderr, flush=True)

    seen_files = {}
    for path_str, ref_paths in state.get("seen_files", {}).items():
        seen_files[path_str] = list(ref_paths)

    _track_files_for_write(tool_name, tool_input, output_text, seen_files)
    state["seen_files"] = seen_files

    now = time.time()
    if state["window_start"] > 0 and (now - state["window_start"]) > TIMEOUT_SECONDS:
        state["window_start"] = now
        state["counter"] = 0
        state["history"] = []

    if not output_text:
        return 0

    turn_entry = {
        "name": tool_name,
        "input": tool_input,
        "output": output_text,
        "files": tool_files,
        "timestamp": timestamp,
        "message_id": message_id,
    }
    state.setdefault("history", []).append(turn_entry)

    state["counter"] = state.get("counter", 0) + 1
    state["window_start"] = state.get("window_start", 0.0) or now

    nudge = _nudge_maybe(state)
    if nudge:
        nudge_text = _format_nudge(nudge)
        print(nudge_text)
        state["last_nudge"] = now
        state["counter"] = 0
        state["history"] = []

    _save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
