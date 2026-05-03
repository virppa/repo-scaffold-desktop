"""Pure helper functions for the watcher sub-system (no I/O, unit-testable).

All functions in this module are stateless and have no self-dependencies.
This module may import from watcher_types only (no other watcher siblings).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import IO

from app.core.manifest import ExecutionManifest

from .watcher_types import (
    _ENV_VARS_TO_STRIP_FOR_CLOUD,
    _LITELLM_BASE_URL,
    _LOCAL_MODEL,
    ActiveWorker,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Worker log parsing
# ---------------------------------------------------------------------------


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
                    usage = (obj.get("message") or {}).get("usage") or {}
                    inp = usage.get("input_tokens")
                    out = usage.get("output_tokens")
                    if inp is not None and out is not None:
                        total_input += int(inp)
                        total_output += int(out)
                        has_assistant_usage = True
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


# ---------------------------------------------------------------------------
# Escalation-policy flag names (also used by watcher.py orchestrator)
# ---------------------------------------------------------------------------

_POLICY_FLAGS = (
    "scope_drift",
    "forbidden_path_touched",
    "import_linter_violation",
    "security_blocker",
)


def _read_result_flags(result_path: Path) -> dict[str, bool]:
    """Load result.json and return the four escalation-policy boolean flags.

    Returns all-False defaults when the file is missing or malformed.
    """
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return dict.fromkeys(_POLICY_FLAGS, False)
    return {f: bool(raw.get(f, False)) for f in _POLICY_FLAGS}


# ---------------------------------------------------------------------------
# Allowed-paths overlap check
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


# ---------------------------------------------------------------------------
# Worker environment and command builders
# ---------------------------------------------------------------------------


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
        # Compact at ~180K tokens: 240K window × 75% PCT trigger.
        # vLLM FP8 throughput is flat 16K→262K (WOR-234/WOR-118), so there is no
        # throughput cliff to avoid — 240K gives generous context while leaving 80K
        # headroom before the 262K hard limit. 75% fires compaction early enough to
        # prevent late-session drift observed in WOR-216/WOR-217/WOR-212 (163K peak).
        env.setdefault("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "240000")
        env.setdefault("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "75")
    return env


def build_worker_cmd(
    ticket_id: str,
    mode: str,
    worktree_path: Path,
    prompt: str | None = None,
    disallowed_tools: list[str] | None = None,
    mcp_config_json: str | None = None,
    *,
    effort: str | None = None,
) -> list[str]:
    """Return the claude subprocess command list for the given mode.

    prompt — pre-expanded skill content; defaults to the /implement-ticket
    slash-command shortcut (requires commands to be loaded by Claude Code).

    disallowed_tools — list of tool-call patterns passed to --disallowed-tools
    (e.g. ["Read(*watcher.py)", "Read(*metrics.py)"]) to enforce context_snippets.

    mcp_config_json — JSON string for --mcp-config. When None, uses an empty
    server map ('{"mcpServers":{}}') to disable all MCP servers. Pass a
    non-empty value to enable specific MCP servers (e.g. Linear).
    """
    if prompt is None:
        prompt = f"/implement-ticket {ticket_id}"
    mcp_config = mcp_config_json if mcp_config_json is not None else '{"mcpServers":{}}'
    base = [
        "claude",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(worktree_path),
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config,
        "--verbose",
        "--output-format",
        "stream-json",
    ]
    if effort is not None:
        _effort = effort
    else:
        _effort = "xhigh" if mode == "local" else "max"
    if mode == "local":
        # (CLI values: low|medium|high|xhigh|max — "normal" was removed)
        # When effort=None, fall back to xhigh for local, max for cloud.
        base += ["--effort", _effort]
        base += ["--model", _LOCAL_MODEL]
    else:
        base += ["--effort", _effort]
    if disallowed_tools:
        base += ["--disallowed-tools", ",".join(disallowed_tools)]
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


# ---------------------------------------------------------------------------
# Worker output tee (runs in a daemon thread)
# ---------------------------------------------------------------------------


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
# Deferral-log suppression
# ---------------------------------------------------------------------------


def suppress_dedup(
    ticket_id: str,
    reason: str,
    reason_msg: str,
    dedup_state: dict[str, str],
) -> str | None:
    """Suppress repeated deferral log messages for the same (ticket, reason).

    *reason* is a stable key (e.g. ``"overlap:WOR-11"`` or ``"local_pool_full"``)
    so the same reason across poll cycles is detected.

    When *reason* changes for a ticket, emits an
    ``<ticket> dispatch unblocked, retrying`` info-line first.

    Returns the message string to log, or ``None`` to suppress.
    """
    last = dedup_state.get(ticket_id)

    if last is not None and last == reason:
        # Same reason as last poll — suppress.
        return None

    # Reason changed (or first time seen) — emit unblock for the old
    # reason if there was one, then record the new reason.
    if last is not None:
        logger.info("%s dispatch unblocked, retrying", ticket_id)

    dedup_state[ticket_id] = reason
    return reason_msg


# ---------------------------------------------------------------------------
# Ollama config parsing
# ---------------------------------------------------------------------------


def _parse_ollama_model(config_path: Path) -> str:
    """Return the bare Ollama model name from a LiteLLM YAML config.

    Scans for the first 'model: ollama_chat/<name>' line and returns <name>.
    Raises ValueError if none is found, FileNotFoundError if the file is absent.
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"LiteLLM config not found: {config_path}. "
            "Copy litellm-local.yaml.example to litellm-local.yaml and configure it."
        )
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r"model:\s+ollama_chat/(\S+)", text)
    if match is None:
        raise ValueError(
            f"No ollama_chat/ model found in {config_path}. "
            "Add a model_list entry with litellm_params.model = 'ollama_chat/<model>'."
        )
    return match.group(1)
