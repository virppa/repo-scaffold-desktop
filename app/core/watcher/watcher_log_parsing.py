"""Pure functions for parsing worker stream-json logs.

Extracted from watcher_helpers.py as part of the LOC-reduction effort (WOR-403).
All functions are stateless, take only a Path argument, and have no self-
dependencies. This module may import from watcher_types only (no other
watcher siblings).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
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
# Public (no leading underscore) so the unified telemetry parser in
# watcher_helpers.py can reference the same set without re-declaring.
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
    """Return the most recent tool call name (or thinking/text snippet) from
    a stream-json log. Returns ``""`` if the log is missing/unparseable.

    Walks the log in a single pass; the last-seen block wins (overwrites
    earlier `result`). For in-progress workers this gives a live view of
    what the LLM is doing.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            result = ""
            for raw in f:
                obj = _parse_assistant_event(raw)
                if obj is None:
                    continue
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    rendered = _render_content_block(block)
                    if rendered is not None:
                        result = rendered
            return result
    except Exception:
        return ""


def _parse_assistant_event(raw_line: str) -> dict[str, Any] | None:
    """Parse one JSONL line; return the dict only when it's an assistant event."""
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    return obj


def _render_content_block(block: dict[str, Any]) -> str | None:
    """Render one assistant content block as the last-action string.

    Returns:
        - tool name for ``tool_use`` blocks (empty string if no name)
        - first ~37 chars of ``thinking``/``text`` for those block types
        - None when the block type doesn't contribute to last-action display

    (None vs empty-string matters: None means "skip", empty means
    "explicitly clear the prior result".)
    """
    btype = block.get("type")
    if btype == "tool_use":
        name = block.get("name", "")
        return name if isinstance(name, str) and name else ""
    if btype in ("thinking", "text"):
        text = block.get("thinking") or block.get("text") or ""
        if not isinstance(text, str) or not text:
            return ""
        # Truncate to ~40 chars to keep the TUI column usable
        return (text[:37] + "...") if len(text) > 40 else text
    return None


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


class WorkerBehavior:
    """Per-worker behavior summary parsed from a stream-json log.

    Concurrency-safe sibling to WOR-370 (server-side /metrics): every value
    here is derived from the worker's own log file, so multiple concurrent
    workers each produce their own attributable summary.

    All fields are Optional to distinguish "definitely zero" from
    "couldn't read the log" (None means the log was missing or unparseable
    at the file-handle level; 0 / {} means the log was readable but the
    feature didn't appear).
    """

    __slots__ = (
        "turn_count",
        "tool_calls_total",
        "tool_calls_breakdown",
        "thinking_blocks",
        "thinking_chars_total",
        "input_tokens_max",
        "input_tokens_first",
        "input_tokens_last",
        "redundant_reads_count",
    )

    def __init__(
        self,
        turn_count: int | None,
        tool_calls_total: int | None,
        tool_calls_breakdown: dict[str, int] | None,
        thinking_blocks: int | None,
        thinking_chars_total: int | None,
        input_tokens_max: int | None,
        input_tokens_first: int | None,
        input_tokens_last: int | None,
        redundant_reads_count: int | None,
    ) -> None:
        self.turn_count = turn_count
        self.tool_calls_total = tool_calls_total
        self.tool_calls_breakdown = tool_calls_breakdown
        self.thinking_blocks = thinking_blocks
        self.thinking_chars_total = thinking_chars_total
        self.input_tokens_max = input_tokens_max
        self.input_tokens_first = input_tokens_first
        self.input_tokens_last = input_tokens_last
        self.redundant_reads_count = redundant_reads_count

    @classmethod
    def empty_unparseable(cls) -> "WorkerBehavior":
        """Sentinel: log unreadable, so every field is None."""
        return cls(None, None, None, None, None, None, None, None, None)

    @classmethod
    def empty_readable(cls) -> "WorkerBehavior":
        """Sentinel: log was readable but had no assistant turns."""
        return cls(0, 0, {}, 0, 0, None, None, None, 0)


def _update_input_tokens(
    inp: object,
    first: int | None,
    last: int | None,
    cur_max: int | None,
) -> tuple[int | None, int | None, int | None]:
    """Fold a single usage.input_tokens value into the running (first, last, max)."""
    if not isinstance(inp, int):
        return first, last, cur_max
    new_first = inp if first is None else first
    new_last = inp
    new_max = inp if cur_max is None else max(cur_max, inp)
    return new_first, new_last, new_max


def _accumulate_content_block(
    block: dict[str, Any],
    behavior_accum: dict[str, int],
    tool_breakdown: dict[str, int],
    read_counts: dict[str, int],
) -> None:
    """Update accumulators for one content block (thinking or tool_use)."""
    btype = block.get("type")
    if btype == "thinking":
        behavior_accum["thinking_blocks"] += 1
        text = block.get("thinking") or block.get("text") or ""
        if isinstance(text, str):
            behavior_accum["thinking_chars"] += len(text)
    elif btype == "tool_use":
        behavior_accum["tool_calls_total"] += 1
        name = block.get("name") or "?"
        tool_breakdown[name] = tool_breakdown.get(name, 0) + 1
        if name == "Read":
            fp = (block.get("input") or {}).get("file_path")
            if isinstance(fp, str) and fp:
                key = fp.replace("\\", "/")
                read_counts[key] = read_counts.get(key, 0) + 1


class WorkerTelemetry:
    """Unified worker-log telemetry walked from one JSONL pass (WOR-466).

    Replaces 5 separate `_parse_worker_*` walks at the finalize site with a
    single traversal. The standalone parsers remain for other call sites
    (heartbeat, ticket_status) that only need one slice.

    Fields mirror the per-parser return types so the finalize_worker call
    site can swap in cleanly:

      * input_tokens / output_tokens / context_compactions / compact_duration_ms
          -> matches `_parse_worker_usage`
      * subagent_spawns          -> matches `_parse_worker_subagent_spawns`
      * api_retries              -> matches `_parse_worker_api_retries`
      * hook_trust_violations    -> matches `_parse_hook_trust_violations`
      * behavior                 -> matches `_parse_worker_behavior`
    """

    __slots__ = (
        "input_tokens",
        "output_tokens",
        "context_compactions",
        "compact_duration_ms",
        "subagent_spawns",
        "api_retries",
        "hook_trust_violations",
        "behavior",
    )

    def __init__(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        context_compactions: int | None,
        compact_duration_ms: int | None,
        subagent_spawns: int | None,
        api_retries: int | None,
        hook_trust_violations: int | None,
        behavior: WorkerBehavior,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.context_compactions = context_compactions
        self.compact_duration_ms = compact_duration_ms
        self.subagent_spawns = subagent_spawns
        self.api_retries = api_retries
        self.hook_trust_violations = hook_trust_violations
        self.behavior = behavior

    @classmethod
    def empty_unparseable(cls) -> "WorkerTelemetry":
        """Sentinel: log unreadable. Mirrors each parser's failure return."""
        return cls(
            input_tokens=None,
            output_tokens=None,
            context_compactions=None,
            compact_duration_ms=None,
            subagent_spawns=None,
            api_retries=None,
            hook_trust_violations=None,
            behavior=WorkerBehavior.empty_unparseable(),
        )


class _TelemetryAccum:
    """Mutable accumulators for one :func:`_parse_worker_telemetry` pass.

    WOR-510 (S3776, CC 47→≤15): the single-pass parser's per-event logic
    is decomposed into ``_handle_*_event`` handlers that mutate this shared
    struct. One field per local the parser used before — same values,
    same mutation order, behaviour identical. ``__slots__`` mirrors the
    WorkerTelemetry style (no new imports).
    """

    __slots__ = (
        "total_input",
        "total_output",
        "has_assistant_usage",
        "compact_count",
        "compact_duration_ms",
        "last_input",
        "last_output",
        "content_chars",
        "subagent_spawns",
        "api_retries",
        "hook_violations",
        "turn_count",
        "behavior_accum",
        "tool_breakdown",
        "input_tokens_first",
        "input_tokens_last",
        "input_tokens_max",
        "read_counts",
    )

    def __init__(self) -> None:
        self.total_input = 0
        self.total_output = 0
        self.has_assistant_usage = False
        self.compact_count = 0
        self.compact_duration_ms = 0
        self.last_input: int | None = None
        self.last_output: int | None = None
        self.content_chars = 0
        self.subagent_spawns = 0
        self.api_retries = 0
        self.hook_violations = 0
        self.turn_count = 0
        self.behavior_accum: dict[str, int] = {
            "thinking_blocks": 0,
            "thinking_chars": 0,
            "tool_calls_total": 0,
        }
        self.tool_breakdown: dict[str, int] = {}
        self.input_tokens_first: int | None = None
        self.input_tokens_last: int | None = None
        self.input_tokens_max: int | None = None
        self.read_counts: dict[str, int] = {}


def _handle_assistant_event(obj: dict[str, Any], acc: _TelemetryAccum) -> None:
    """Apply one ``type=="assistant"`` event (WOR-510 — verbatim from the
    former inline branch; usage + behavior + spawns + hook violations)."""
    # --- usage ---
    i_d, o_d, has_u, cc = _process_assistant_event(obj)
    acc.total_input += i_d
    acc.total_output += o_d
    acc.has_assistant_usage = acc.has_assistant_usage or has_u
    acc.content_chars += cc

    # --- behavior ---
    acc.turn_count += 1
    msg = obj.get("message") or {}
    usage = msg.get("usage") or {}
    (
        acc.input_tokens_first,
        acc.input_tokens_last,
        acc.input_tokens_max,
    ) = _update_input_tokens(
        usage.get("input_tokens"),
        acc.input_tokens_first,
        acc.input_tokens_last,
        acc.input_tokens_max,
    )

    # --- behavior content + spawns + hook violations ---
    for blk in msg.get("content") or []:
        if not isinstance(blk, dict):
            continue
        _accumulate_content_block(
            blk, acc.behavior_accum, acc.tool_breakdown, acc.read_counts
        )
        if blk.get("type") == "tool_use":
            name = blk.get("name")
            if name == "Task":
                acc.subagent_spawns += 1
            elif name == "Bash":
                token = _is_violation_bash_command(blk)
                if token and token in _HOOK_VIOLATION_TOKENS:
                    acc.hook_violations += 1


def _handle_system_event(obj: dict[str, Any], acc: _TelemetryAccum) -> None:
    """Apply one ``type=="system"`` event (compact_boundary / api_retry)."""
    subtype = obj.get("subtype")
    if subtype == "compact_boundary":
        c_d, d_d = _process_compact_boundary(obj)
        acc.compact_count += c_d
        acc.compact_duration_ms += d_d
    elif subtype == "api_retry":
        acc.api_retries += 1


def _handle_result_event(obj: dict[str, Any], acc: _TelemetryAccum) -> None:
    """Apply one ``type=="result"`` event (final usage totals)."""
    usage = obj.get("usage") or {}
    acc.last_input = usage.get("input_tokens")
    acc.last_output = usage.get("output_tokens")


def _parse_worker_telemetry(log_path: Path) -> WorkerTelemetry:
    """Walk the worker JSONL log once and extract all telemetry (WOR-466).

    On a typical 2-MB log this is ~5x faster than calling the 5 individual
    parsers sequentially (one disk read + one json.loads per line vs five).
    On large logs (~200 MB) the absolute saving is ~3-4 seconds.

    Behavior matches the per-parser return types exactly — see WorkerTelemetry
    docstring for the field mapping. On unreadable logs returns
    WorkerTelemetry.empty_unparseable() (every field None / sentinel),
    mirroring each old parser's failure return.
    """
    acc = _TelemetryAccum()

    try:
        with log_path.open(encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                obj_type = obj.get("type")
                if obj_type == "assistant":
                    _handle_assistant_event(obj, acc)
                elif obj_type == "system":
                    _handle_system_event(obj, acc)
                elif obj_type == "result":
                    _handle_result_event(obj, acc)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Failed to read %s for unified telemetry — falling back to "
            "empty_unparseable; %s",
            log_path,
            exc,
            exc_info=True,
        )
        return WorkerTelemetry.empty_unparseable()

    input_tok, output_tok, compactions, compact_dur = _resolve_usage_totals(
        acc.total_input,
        acc.total_output,
        acc.has_assistant_usage,
        acc.compact_count,
        acc.compact_duration_ms,
        acc.last_input,
        acc.last_output,
        acc.content_chars,
    )

    if acc.turn_count == 0:
        behavior = WorkerBehavior.empty_readable()
    else:
        redundant = sum(1 for n in acc.read_counts.values() if n > 2)
        behavior = WorkerBehavior(
            turn_count=acc.turn_count,
            tool_calls_total=acc.behavior_accum["tool_calls_total"],
            tool_calls_breakdown=acc.tool_breakdown,
            thinking_blocks=acc.behavior_accum["thinking_blocks"],
            thinking_chars_total=acc.behavior_accum["thinking_chars"],
            input_tokens_max=acc.input_tokens_max,
            input_tokens_first=acc.input_tokens_first,
            input_tokens_last=acc.input_tokens_last,
            redundant_reads_count=redundant,
        )

    return WorkerTelemetry(
        input_tokens=input_tok,
        output_tokens=output_tok,
        context_compactions=compactions,
        compact_duration_ms=compact_dur,
        subagent_spawns=acc.subagent_spawns,
        api_retries=acc.api_retries,
        hook_trust_violations=acc.hook_violations,
        behavior=behavior,
    )


def _parse_worker_behavior(log_path: Path) -> WorkerBehavior:
    """Extract per-session behavior signals from the stream-json worker log.

    Counts turns, tool calls + breakdown, thinking blocks + chars, and the
    input_tokens trajectory (first/last/max). Derives redundant_reads_count
    from per-file Read counts. Returns ``empty_unparseable()`` on read/parse
    failure; ``empty_readable()`` if parsed but no assistant turns.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            return _walk_behavior_log(f)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Failed to read %s — behaviour telemetry columns will be NULL; %s",
            log_path,
            exc,
            exc_info=True,
        )
        return WorkerBehavior.empty_unparseable()


def _load_assistant_event(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line; return dict only if it is an assistant event."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    return obj


def _walk_behavior_log(lines: Iterable[str]) -> WorkerBehavior:
    """Aggregate per-session behavior signals from a stream of JSONL lines."""
    turn_count = 0
    behavior_accum = {
        "thinking_blocks": 0,
        "thinking_chars": 0,
        "tool_calls_total": 0,
    }
    tool_breakdown: dict[str, int] = {}
    input_tokens_first: int | None = None
    input_tokens_last: int | None = None
    input_tokens_max: int | None = None
    read_counts: dict[str, int] = {}
    for raw in lines:
        obj = _load_assistant_event(raw)
        if obj is None:
            continue
        turn_count += 1
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        input_tokens_first, input_tokens_last, input_tokens_max = _update_input_tokens(
            usage.get("input_tokens"),
            input_tokens_first,
            input_tokens_last,
            input_tokens_max,
        )
        for blk in msg.get("content") or []:
            if isinstance(blk, dict):
                _accumulate_content_block(
                    blk, behavior_accum, tool_breakdown, read_counts
                )
    if turn_count == 0:
        return WorkerBehavior.empty_readable()
    redundant = sum(1 for n in read_counts.values() if n > 2)
    return WorkerBehavior(
        turn_count=turn_count,
        tool_calls_total=behavior_accum["tool_calls_total"],
        tool_calls_breakdown=tool_breakdown,
        thinking_blocks=behavior_accum["thinking_blocks"],
        thinking_chars_total=behavior_accum["thinking_chars"],
        input_tokens_max=input_tokens_max,
        input_tokens_first=input_tokens_first,
        input_tokens_last=input_tokens_last,
        redundant_reads_count=redundant,
    )
