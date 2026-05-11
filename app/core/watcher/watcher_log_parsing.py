"""Pure functions for parsing worker stream-json logs.

Extracted from watcher_helpers.py as part of the LOC-reduction effort (WOR-403).
All functions are stateless, take only a Path argument, and have no self-
dependencies. This module may import from watcher_types only (no other
watcher siblings).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Worker log parsing
# ---------------------------------------------------------------------------


# WOR-384: chars/token ratio used when falling back to content-length
# estimation. Empirically derived from English + code mix in worker logs.
# Slightly conservative to avoid over-estimating output volume.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _block_text_chars(block: dict[str, Any]) -> int:
    """Approximate character length of one content block (WOR-384 sentinel)."""
    btype = block.get("type")
    if btype in ("thinking", "text"):
        key = "thinking" if btype == "thinking" else "text"
        text = block.get(key) or block.get("text") or ""
        return len(text) if isinstance(text, str) else 0
    if btype != "tool_use":
        return 0
    inp_dict = block.get("input")
    if inp_dict is None:
        return 0
    try:
        return len(json.dumps(inp_dict))
    except (TypeError, ValueError):
        return 0


def _accumulate_content_chars(content: Any) -> int:
    """Sum approximate character lengths across content blocks."""
    if not isinstance(content, list):
        return 0
    return sum(_block_text_chars(b) for b in content if isinstance(b, dict))


def _process_assistant_event(obj: dict[str, Any]) -> tuple[int, int, bool, int]:
    """Extract (input_delta, output_delta, has_usage, content_chars) from one event."""
    msg = obj.get("message") or {}
    usage = msg.get("usage") or {}
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if inp is not None and out is not None:
        input_delta = int(inp)
        output_delta = int(out)
        has_usage = True
    else:
        input_delta = output_delta = 0
        has_usage = False
    content_chars = _accumulate_content_chars(msg.get("content"))
    return input_delta, output_delta, has_usage, content_chars


def _process_compact_boundary(obj: dict[str, Any]) -> tuple[int, int]:
    """Extract (count_delta=1, duration_delta) from a compact_boundary event."""
    meta = obj.get("compact_metadata") or {}
    dur = meta.get("duration_ms")
    return 1, int(dur) if isinstance(dur, (int, float)) else 0


def _resolve_usage_totals(
    total_input: int,
    total_output: int,
    has_assistant_usage: bool,
    compact_count: int,
    compact_duration_ms: int,
    last_input: int | None,
    last_output: int | None,
    content_chars: int,
) -> tuple[int | None, int | None, int | None, int | None]:
    """Apply WOR-384 sentinel + result-fallback rules to produce the final tuple."""
    if has_assistant_usage:
        if total_output == 0 and total_input > 0 and content_chars > 0:
            estimated = content_chars // _CHARS_PER_TOKEN_ESTIMATE
            return total_input, estimated, compact_count, compact_duration_ms
        return total_input, total_output, compact_count, compact_duration_ms
    if last_input is not None and last_output is not None:
        return int(last_input), int(last_output), compact_count, compact_duration_ms
    return None, None, compact_count, compact_duration_ms


def _parse_worker_usage(
    log_path: Path,
) -> tuple[int | None, int | None, int | None, int | None]:
    """Return (input_tokens, output_tokens, context_compactions, compact_duration_ms).

    Sums assistant-turn deltas. Counts compact_boundary system events
    (WOR-357) and their durations (WOR-358). WOR-384 sentinel: when
    output_tokens is 0 across all assistant events but input_tokens > 0,
    estimates output from content character length / _CHARS_PER_TOKEN_ESTIMATE.
    Falls back to the last result event's usage snapshot if no assistant
    events carry usage. Returns (None, None, None, None) on read/parse failure.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            total_input = total_output = 0
            has_assistant_usage = False
            compact_count = compact_duration_ms = 0
            last_input: int | None = None
            last_output: int | None = None
            content_chars = 0
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                obj_type = obj.get("type")
                if obj_type == "assistant":
                    i_d, o_d, has_u, cc = _process_assistant_event(obj)
                    total_input += i_d
                    total_output += o_d
                    has_assistant_usage = has_assistant_usage or has_u
                    content_chars += cc
                elif obj_type == "system" and obj.get("subtype") == "compact_boundary":
                    c_d, d_d = _process_compact_boundary(obj)
                    compact_count += c_d
                    compact_duration_ms += d_d
                elif obj_type == "result":
                    usage = obj.get("usage") or {}
                    last_input = usage.get("input_tokens")
                    last_output = usage.get("output_tokens")
            return _resolve_usage_totals(
                total_input,
                total_output,
                has_assistant_usage,
                compact_count,
                compact_duration_ms,
                last_input,
                last_output,
                content_chars,
            )
    except Exception:
        return None, None, None, None


def _parse_worker_subagent_spawns(log_path: Path) -> int | None:
    """Count Task-tool invocations in the worker log (WOR-364).

    Each Task tool_use spawns a subagent (a separate Claude Code session
    with its own LLM stream). High counts contribute to vLLM concurrency
    even when the watcher's pool only shows one active worker.

    Returns ``None`` if the log cannot be opened/parsed; ``0`` for
    parseable logs with no Task tool_use events.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            count = 0
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {}) or {}
                for block in msg.get("content", []):
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "Task"
                    ):
                        count += 1
            return count
    except Exception:
        return None


def _parse_worker_api_retries(log_path: Path) -> int | None:
    """Count ``type=system, subtype=api_retry`` events in the worker log (WOR-360).

    Each event represents a transient API failure that Claude Code retried
    internally. Useful as a backend-stability proxy — sessions with many
    retries correlate with degraded vLLM/LiteLLM throughput.

    Returns ``None`` if the log cannot be opened/parsed; ``0`` for
    parseable logs with no retry events.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            count = 0
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "system" and obj.get("subtype") == "api_retry":
                    count += 1
            return count
    except Exception:
        return None


# WOR-274: commands whose manual invocation during step 3 is a hook-trust
# violation. Case-sensitive — 'Ruff' does not match 'ruff'.
_HOOK_VIOLATION_TOKENS = frozenset(
    ("ruff", "mypy", "pytest", "bandit", "lint-imports"),
)


def _is_violation_bash_command(block: Any) -> str | None:
    """Return the first token of a Bash command in a tool_use block, else None."""
    if (
        not isinstance(block, dict)
        or block.get("type") != "tool_use"
        or block.get("name") != "Bash"
    ):
        return None
    command = (block.get("input") or {}).get("command", "")
    if not isinstance(command, str):
        return None
    return command.lstrip().split()[0] if command.lstrip() else None


def _count_violations_in_event(obj: dict[str, Any]) -> int:
    """Count violation tokens in a single assistant event's tool_use blocks."""
    if obj.get("type") != "assistant":
        return 0
    msg = obj.get("message", {}) or {}
    count = 0
    for block in msg.get("content", []):
        token = _is_violation_bash_command(block)
        if token and token in _HOOK_VIOLATION_TOKENS:
            count += 1
    return count


def _parse_hook_trust_violations(log_path: Path) -> int | None:
    """Count manual quality-check tool invocations in Bash tool_use events.

    Targets ruff/mypy/pytest/bandit/lint-imports. Count > 1 = hook-trust
    violation per CLAUDE.md / WOR-274.

    Returns None on read/parse failure; 0 for parseable logs with no matches.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            count = 0
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += _count_violations_in_event(obj)
            return count
    except Exception:
        return None


def format_token_count(total: int) -> str:
    """Format a token count for display: ``142k`` for >= 1000, raw integer below."""
    if total < 1000:
        return str(total)
    k = total / 1000
    if k == int(k):
        return f"{int(k)}k"
    return f"{k:.0f}k"


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as ``5m12s`` (integer seconds)."""
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{mins}m{secs:02d}s"


def last_tool_call(log_path: Path) -> str:
    """Return the most recent tool call name from a stream-json log.

    Walks the log in a single pass and returns the ``name`` of the last
    ``type=tool_use`` content block (e.g. ``Read``, ``Bash``, ``Edit``).
    Returns the most recent thinking/text summary when the last block was
    a ``thinking`` or ``text`` type. Returns ``""`` when the log is missing,
    unparseable, or has no assistant messages yet (running worker with no
    assistant turns).

    For in-progress workers this gives a live view of what the LLM is doing.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            result = ""
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name", "")
                        if isinstance(name, str) and name:
                            result = name
                        else:
                            result = ""
                    elif btype in ("thinking", "text"):
                        text = (
                            block.get("thinking")
                            or block.get("text")
                            or block.get("text", "")
                        )
                        if isinstance(text, str) and text:
                            # Truncate to ~40 chars to keep the TUI column usable
                            result = (text[:37] + "...") if len(text) > 40 else text
                        else:
                            result = ""
            return result
    except Exception:
        return ""


def format_worker_token_count(log_path: Path) -> str:
    """Return ``142k tokens`` for the worker log, ``? tokens`` if unknown.

    ``_parse_worker_usage`` already swallows its own errors and returns
    ``(None, None, None, None)`` when the log is missing or malformed, so
    callers do not need to wrap this in try/except.
    """
    input_tok, output_tok, _, _ = _parse_worker_usage(log_path)
    if input_tok is None or output_tok is None:
        return "? tokens"
    return f"{format_token_count(input_tok + output_tok)} tokens"
