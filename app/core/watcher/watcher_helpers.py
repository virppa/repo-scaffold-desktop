"""Pure helper functions for the watcher sub-system (no I/O, unit-testable).

All functions in this module are stateless and have no self-dependencies.
This module may import from watcher_types only (no other watcher siblings).
"""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404
from pathlib import Path
from typing import IO

from app.core.manifest import ExecutionManifest

from .watcher_log_parsing import (
    _parse_worker_api_retries,
    _parse_worker_subagent_spawns,
    _parse_worker_usage,
    format_elapsed,
    format_token_count,
    format_worker_token_count,
)

__all__ = [
    "_POLICY_FLAGS",
    "_read_result_flags",
    "_parse_worker_usage",
    "_parse_worker_subagent_spawns",
    "_parse_worker_api_retries",
    "format_token_count",
    "format_elapsed",
    "format_worker_token_count",
    "check_allowed_paths_overlap",
    "build_worker_env",
    "build_worker_cmd",
    "resolve_effective_mode",
    "_tee_worker_output",
    "suppress_dedup",
    "count_main_ahead_of_epic",
    "capture_vllm_metrics",
    "compute_vllm_metrics_delta",
    "WorkerBehavior",
    "_parse_worker_behavior",
]
from .watcher_types import (
    _ENV_VARS_TO_STRIP_FOR_CLOUD,
    _VLLM_BASE_URL,
    _VLLM_SERVED_MODEL,
    ActiveWorker,
)

logger = logging.getLogger(__name__)

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


def _is_append_only_path(path: str) -> bool:
    """Return True for path entries treated as append-only re-export barrels.

    Two workers editing the same ``__init__.py`` typically only add a new
    import + ``__all__`` entry for their respective new module — commutative
    edits whose only conflict surface is the auto-merge at sub-ticket → epic
    time, which is recoverable. Treating ``__init__.py`` as append-only lets
    package-split tickets dispatch concurrently (WOR-410). If a real conflict
    materialises, one sub-ticket PR will fail to auto-merge and need a manual
    rebase — same outcome as any other late-stage merge collision.
    """
    return path == "__init__.py" or path.endswith("/__init__.py")


def check_allowed_paths_overlap(
    active: list[ActiveWorker], candidate: ExecutionManifest
) -> list[str]:
    """Return identifiers of active workers whose allowed_paths overlap with candidate.

    Two manifests overlap when they share at least one allowed_path pattern,
    excluding append-only barrel files (see ``_is_append_only_path``). An empty
    allowed_paths list means "no restriction" — treated as overlap with
    everything to be safe.
    """
    if not candidate.allowed_paths:
        return [w.manifest.ticket_id for w in active]

    conflicts: list[str] = []
    candidate_set = set(candidate.allowed_paths)
    for worker in active:
        if not worker.manifest.allowed_paths:
            conflicts.append(worker.manifest.ticket_id)
            continue
        intersection = candidate_set & set(worker.manifest.allowed_paths)
        if any(not _is_append_only_path(p) for p in intersection):
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
    local   — points ANTHROPIC_BASE_URL at vLLM's native Anthropic Messages
              endpoint and pins all three model-tier defaults
              (ANTHROPIC_DEFAULT_OPUS_MODEL / _SONNET_MODEL / _HAIKU_MODEL) to
              the vLLM-served name so Claude Code routes by tier without
              needing --model on the command line. ANTHROPIC_API_KEY /
              ANTHROPIC_AUTH_TOKEN are set to "dummy" only to satisfy Claude
              Code's local auth check; vLLM does not validate.
    default — passes base_env unchanged.
    """
    env = dict(base_env)
    if mode == "cloud":
        for var in _ENV_VARS_TO_STRIP_FOR_CLOUD:
            env.pop(var, None)
        # WOR-391: signal to downstream tests that they're running inside the
        # watcher's worker subprocess. Tests that interact with bash, system
        # PATH-resolved binaries, or other env-divergent code paths can use
        # this flag to skip themselves and avoid env-dependent flakiness.
        # Set in cloud + local branches; default branch is a no-op fallback.
        env["WATCHER_WORKER"] = "1"
    elif mode == "local":
        env["ANTHROPIC_BASE_URL"] = _VLLM_BASE_URL
        env.setdefault("ANTHROPIC_API_KEY", "dummy")
        env.setdefault("ANTHROPIC_AUTH_TOKEN", "dummy")
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = _VLLM_SERVED_MODEL
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = _VLLM_SERVED_MODEL
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = _VLLM_SERVED_MODEL
        # WOR-391: see cloud branch above for rationale.
        env["WATCHER_WORKER"] = "1"
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
    # Local-mode does not pass --model: vLLM's /v1/models endpoint only lists
    # _VLLM_SERVED_MODEL ("qwen3-coder"), so a hard-coded "claude-sonnet-4-6"
    # would fail Claude Code's model-existence validation. Routing happens via
    # ANTHROPIC_DEFAULT_*_MODEL env vars in build_worker_env instead.
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
# Stale-epic detection (WOR-373)
# ---------------------------------------------------------------------------


def count_main_ahead_of_epic(epic_branch: str, repo_root: Path) -> int:
    """Return how many commits ``origin/main`` has that ``epic_branch`` doesn't.

    Used by dispatch to refuse sub-ticket launches against epic branches that
    have drifted too far from main. A long-lived epic branch silently sheds
    main-side changes during periodic merge conflict resolutions; the WOR-282
    forensic uncovered 13 lost tests on a 107-commit-behind epic.

    Returns 0 (no drift) if:
      * ``epic_branch`` does not start with ``epic/`` (only epic branches
        are subject to the staleness check; sub-tickets targeting main
        directly are by definition not stale).
      * ``git fetch`` or ``git rev-list`` fails for any reason (fail open
        — do not block dispatch on infra glitches).

    Otherwise returns the integer commit count.
    """
    if not epic_branch.startswith("epic/"):
        return 0

    # Best-effort fetch so the count reflects the latest origin state.
    # We fetch both refs explicitly; failures are non-fatal (a stale
    # local view simply means the count is conservative, not wrong).
    try:
        subprocess.run(  # nosec B603 B607
            ["git", "fetch", "origin", "main", epic_branch],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    try:
        out = subprocess.check_output(  # nosec B603 B607
            ["git", "rev-list", "--count", f"origin/{epic_branch}..origin/main"],
            cwd=str(repo_root),
            text=True,
            timeout=10,
        )
        return int(out.strip())
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        return 0


# ---------------------------------------------------------------------------
# vLLM /metrics capture (WOR-370)
# ---------------------------------------------------------------------------

# Counters and gauges to snapshot. Keep in one place so capture and delta
# parsing stay in sync. Each entry: (metric_name, kind) where kind is
# "counter" (cumulative; produce delta = after-before) or "gauge" (point-in-
# time; produce just `before` and `after` snapshots, no delta).
_VLLM_COUNTERS = (
    "vllm:prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:time_to_first_token_seconds_sum",
    "vllm:time_to_first_token_seconds_count",
    "vllm:num_preemptions_total",
)


def capture_vllm_metrics(timeout: float = 5.0) -> dict[str, float] | None:
    """Snapshot the small subset of vLLM Prometheus metrics needed for
    per-ticket attribution.

    Returns a dict keyed by metric name (without label suffixes) mapping to
    the most recent value, or ``None`` if the endpoint is unreachable or
    returns malformed text. Failure is non-fatal — callers treat ``None``
    as "no snapshot, skip attribution."

    Prometheus text format lines look like:
        vllm:prefix_cache_hits_total{model="qwen3-coder",engine="0"} 12345.0

    We strip everything after the metric name, sum across labels (there's
    typically just one model/engine combo, but be safe), and emit a flat dict.

    Uses ``http.client`` (matching ``watcher_services.probe_vllm_health``)
    rather than ``urllib.request`` — the latter accepts ``file://`` URLs
    so semgrep flags any urlopen call. This API only speaks HTTP to a
    fixed localhost host:port pair so there is no SSRF surface.
    """
    import http.client

    # _VLLM_BASE_URL has the form "http://localhost:8000"; strip the scheme
    # and split host:port for http.client.HTTPConnection.
    netloc = _VLLM_BASE_URL.split("://", 1)[1]
    host, _, port_str = netloc.partition(":")
    port = int(port_str) if port_str else 80

    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/metrics")
            resp = conn.getresponse()
            if resp.status != 200:
                return None
            body = resp.read().decode("utf-8", errors="replace")
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return None

    targets = set(_VLLM_COUNTERS)
    aggregated: dict[str, float] = dict.fromkeys(targets, 0.0)
    seen: dict[str, bool] = dict.fromkeys(targets, False)

    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Metric name extends from start to first '{' or whitespace
        for ch_idx, ch in enumerate(line):
            if ch == "{" or ch.isspace():
                metric_name = line[:ch_idx]
                rest = line[ch_idx:]
                break
        else:
            continue
        if metric_name not in targets:
            continue
        # Value is the LAST whitespace-separated token
        try:
            value = float(rest.rsplit(maxsplit=1)[-1])
        except (ValueError, IndexError):
            continue
        aggregated[metric_name] += value
        seen[metric_name] = True

    # If we saw none of the target metrics, treat as failure — endpoint
    # responded but probably isn't a vLLM /metrics endpoint.
    if not any(seen.values()):
        return None
    return aggregated


def compute_vllm_metrics_delta(
    before: dict[str, float],
    after: dict[str, float],
) -> dict[str, float | None]:
    """Compute deltas + derived ratios from two snapshots taken via
    ``capture_vllm_metrics``.

    Counter deltas can be negative if vLLM was restarted between the two
    snapshots (counters reset on restart). In that case the delta is
    meaningless; callers should treat ratios as None when raw counts go
    negative.

    Returns a flat dict suitable for direct assignment to TicketMetrics
    columns (keys match the column names without the ``vllm_`` prefix
    being added by the caller).
    """

    def _delta(key: str) -> float:
        return float(after.get(key, 0.0)) - float(before.get(key, 0.0))

    hits = _delta("vllm:prefix_cache_hits_total")
    queries = _delta("vllm:prefix_cache_queries_total")
    prompt = _delta("vllm:prompt_tokens_total")
    gen = _delta("vllm:generation_tokens_total")
    ttft_sum = _delta("vllm:time_to_first_token_seconds_sum")
    ttft_count = _delta("vllm:time_to_first_token_seconds_count")
    preempt = _delta("vllm:num_preemptions_total")

    # Negative delta = vLLM restarted between snapshots; flag as
    # non-attributable by setting all derived to None.
    counter_corrupt = any(
        v < 0 for v in (hits, queries, prompt, gen, ttft_sum, ttft_count, preempt)
    )

    hit_ratio: float | None = None
    if not counter_corrupt and queries > 0:
        hit_ratio = hits / queries

    ttft_mean: float | None = None
    if not counter_corrupt and ttft_count > 0:
        ttft_mean = ttft_sum / ttft_count

    if counter_corrupt:
        return {
            "prefix_cache_hits": None,
            "prefix_cache_queries": None,
            "prefix_cache_hit_ratio": None,
            "prompt_tokens": None,
            "generation_tokens": None,
            "ttft_seconds_sum": None,
            "ttft_count": None,
            "ttft_mean_seconds": None,
            "preemptions": None,
        }

    return {
        "prefix_cache_hits": int(hits),
        "prefix_cache_queries": int(queries),
        "prefix_cache_hit_ratio": hit_ratio,
        "prompt_tokens": int(prompt),
        "generation_tokens": int(gen),
        "ttft_seconds_sum": ttft_sum,
        "ttft_count": int(ttft_count),
        "ttft_mean_seconds": ttft_mean,
        "preemptions": int(preempt),
    }


# ---------------------------------------------------------------------------
# Per-worker behavior telemetry (WOR-380)
# ---------------------------------------------------------------------------


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


def _parse_worker_behavior(log_path: Path) -> WorkerBehavior:
    """Extract per-session behavior signals from the stream-json worker log.

    Counts assistant turns, tool-call totals + breakdown by name, thinking
    blocks + their text length, and per-file Read counts to derive a
    redundant-reads count. Also captures the input_tokens trajectory
    (first / last / max) which is a proxy for context-window pressure.

    Returns a WorkerBehavior with None fields if the log is missing or
    cannot be opened. Returns zero-valued fields if the log is parseable
    but has no assistant turns.
    """
    try:
        with log_path.open(encoding="utf-8") as f:
            turn_count = 0
            tool_calls_total = 0
            tool_breakdown: dict[str, int] = {}
            thinking_blocks = 0
            thinking_chars = 0
            input_tokens_first: int | None = None
            input_tokens_last: int | None = None
            input_tokens_max: int | None = None
            read_counts: dict[str, int] = {}
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
                turn_count += 1
                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                inp = usage.get("input_tokens")
                if isinstance(inp, int):
                    if input_tokens_first is None:
                        input_tokens_first = inp
                    input_tokens_last = inp
                    input_tokens_max = (
                        inp if input_tokens_max is None else max(input_tokens_max, inp)
                    )
                for blk in msg.get("content") or []:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "thinking":
                        thinking_blocks += 1
                        text = blk.get("thinking") or blk.get("text") or ""
                        if isinstance(text, str):
                            thinking_chars += len(text)
                    elif btype == "tool_use":
                        tool_calls_total += 1
                        name = blk.get("name") or "?"
                        tool_breakdown[name] = tool_breakdown.get(name, 0) + 1
                        if name == "Read":
                            fp = (blk.get("input") or {}).get("file_path")
                            if isinstance(fp, str) and fp:
                                # Normalize Windows back-slashes for grouping
                                key = fp.replace("\\", "/")
                                read_counts[key] = read_counts.get(key, 0) + 1
            if turn_count == 0:
                return WorkerBehavior.empty_readable()
            redundant = sum(1 for n in read_counts.values() if n > 2)
            return WorkerBehavior(
                turn_count=turn_count,
                tool_calls_total=tool_calls_total,
                tool_calls_breakdown=tool_breakdown,
                thinking_blocks=thinking_blocks,
                thinking_chars_total=thinking_chars,
                input_tokens_max=input_tokens_max,
                input_tokens_first=input_tokens_first,
                input_tokens_last=input_tokens_last,
                redundant_reads_count=redundant,
            )
    except (OSError, ValueError) as exc:
        logger.warning(
            "Failed to read %s — behaviour telemetry columns will be NULL; %s",
            log_path,
            exc,
            exc_info=True,
        )
        return WorkerBehavior.empty_unparseable()
