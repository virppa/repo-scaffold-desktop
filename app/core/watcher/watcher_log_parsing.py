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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Worker log parsing
# ---------------------------------------------------------------------------


# WOR-384: chars/token ratio used when falling back to content-length
# estimation. Empirically derived from English + code mix in worker logs.
# Slightly conservative to avoid over-estimating output volume.
_CHARS_PER_TOKEN_ESTIMATE = 4


def _parse_worker_usage(
    log_path: Path,
) -> tuple[int | None, int | None, int | None, int | None]:
    """Return 4-tuple from the stream-json worker log:
    ``(input_tokens, output_tokens, context_compactions, compact_duration_ms)``.

    Assistant-turn deltas are summed so that downstream metrics
    (``local_output_tokens``, ``output_tokens_per_wall_second``) reflect the
    true token volume of a session.

    *context_compactions* is the count of ``compact_boundary`` system events
    (WOR-357). *compact_duration_ms* (WOR-358) is the sum of
    ``compact_metadata.duration_ms`` across those events — total wall time
    spent on compaction during the session.

    The actual compaction signal lives in events of the form::

        {"type":"system","subtype":"compact_boundary",
         "compact_metadata":{"trigger":"auto","pre_tokens":...,"post_tokens":...,
                             "duration_ms":...}}

    When assistant events carry usage data, their cumulative sum is returned.
    When no assistant events have usage, the last ``type=result`` event's
    usage snapshot is used as a fallback for tokens. Returns
    ``(None, None, None, None)`` when the log itself cannot be opened or
    parsed at all; returns ``(in, out, 0, 0)`` for parseable logs containing
    zero compact_boundary events.

    *vLLM-direct sentinel (WOR-384):* when assistant events report
    ``output_tokens: 0`` for every turn but ``input_tokens > 0`` (the
    post-WOR-368 vLLM-direct path emits that pattern because vLLM doesn't
    fill per-message output token counts), we fall back to estimating output
    from the assistant content blocks' character lengths divided by
    ``_CHARS_PER_TOKEN_ESTIMATE``. The estimate is rough (±20% typical) but
    drastically better than the alternative of recording 0. Forward-
    compatible: when vLLM starts populating real output_tokens, the sentinel
    won't trigger.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            total_input = 0
            total_output = 0
            has_assistant_usage = False
            compact_count = 0
            compact_duration_ms = 0
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
                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    inp = usage.get("input_tokens")
                    out = usage.get("output_tokens")
                    if inp is not None and out is not None:
                        total_input += int(inp)
                        total_output += int(out)
                        has_assistant_usage = True
                    # Always accumulate content chars for the WOR-384 sentinel
                    # fallback. Cheap to compute; only used when the primary
                    # output_tokens signal is unreliable.
                    for blk in msg.get("content") or []:
                        if not isinstance(blk, dict):
                            continue
                        btype = blk.get("type")
                        if btype == "thinking":
                            text = blk.get("thinking") or blk.get("text") or ""
                            if isinstance(text, str):
                                content_chars += len(text)
                        elif btype == "text":
                            text = blk.get("text") or ""
                            if isinstance(text, str):
                                content_chars += len(text)
                        elif btype == "tool_use":
                            inp_dict = blk.get("input")
                            if inp_dict is not None:
                                try:
                                    content_chars += len(json.dumps(inp_dict))
                                except (TypeError, ValueError):
                                    pass
                elif obj_type == "system" and obj.get("subtype") == "compact_boundary":
                    compact_count += 1
                    meta = obj.get("compact_metadata") or {}
                    dur = meta.get("duration_ms")
                    if isinstance(dur, (int, float)):
                        compact_duration_ms += int(dur)
                elif obj_type == "result":
                    usage = obj.get("usage") or {}
                    last_input = usage.get("input_tokens")
                    last_output = usage.get("output_tokens")
            if has_assistant_usage:
                # WOR-384 sentinel: vLLM-direct emits output_tokens=0 in every
                # assistant.message.usage. Detect via "input>0 but output is
                # exactly 0 across all events" and fall back to a content-
                # length estimate. Anthropic API and any future vLLM that
                # fills output_tokens correctly will skip this branch.
                if total_output == 0 and total_input > 0 and content_chars > 0:
                    estimated = content_chars // _CHARS_PER_TOKEN_ESTIMATE
                    return total_input, estimated, compact_count, compact_duration_ms
                return total_input, total_output, compact_count, compact_duration_ms
            # Fallback to result snapshot when no assistant events carry usage.
            if last_input is not None and last_output is not None:
                return (
                    int(last_input),
                    int(last_output),
                    compact_count,
                    compact_duration_ms,
                )
            return None, None, compact_count, compact_duration_ms
    except Exception:
        return None, None, None, None
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


def _parse_hook_trust_violations(log_path: Path) -> int | None:
    """Count manual invocations of quality-check tools as Bash tool_use events.

    Scans the worker stream-json log for ``type=assistant`` events whose
    ``content`` includes a ``Bash`` tool_use block.  The first whitespace-
    delimited token of the command (after stripping leading whitespace) is
    compared against ``{"ruff", "mypy", "pytest", "bandit", "lint-imports"}``
    (case-sensitive).  Each match increments the count.

    This detects when a worker manually re-runs checks that should only be
    triggered by PostToolUse hooks (step 3 of /implement-ticket).  A count
    greater than 1 is considered a hook-trust violation.

    Returns ``None`` if the log cannot be opened/parsed; ``0`` for
    parseable logs with no matching Bash tool_use events.
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
                        not isinstance(block, dict)
                        or block.get("type") != "tool_use"
                        or block.get("name") != "Bash"
                    ):
                        continue
                    command = (block.get("input") or {}).get("command", "")
                    if not isinstance(command, str):
                        continue
                    first_token = (
                        command.lstrip().split()[0] if command.lstrip() else ""
                    )
                    if first_token in _HOOK_VIOLATION_TOKENS:
                        count += 1
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
