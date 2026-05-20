"""Pure helper functions for the watcher sub-system (no I/O, unit-testable).

All functions in this module are stateless and have no self-dependencies.
This module may import from watcher_types only (no other watcher siblings).
"""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from app.core.manifest import ExecutionManifest

# WOR-510 PR-b: the telemetry-parse cluster (WorkerBehavior/Telemetry,
# _parse_worker_telemetry/_behavior + their handlers) moved DOWN into
# watcher_log_parsing to take watcher_helpers back under the 1200-LOC
# block gate. Re-exported here for API stability — same facade pattern
# the WOR-403 split already uses for the standalone parsers below.
from .watcher_log_parsing import (
    WorkerBehavior,
    WorkerTelemetry,
    _parse_hook_trust_violations,
    _parse_worker_api_retries,
    _parse_worker_behavior,
    _parse_worker_subagent_spawns,
    _parse_worker_telemetry,
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
    "_parse_hook_trust_violations",
    "_parse_worker_api_retries",
    "format_token_count",
    "format_elapsed",
    "format_worker_token_count",
    "check_allowed_paths_overlap",
    "get_active_parent_ids",
    "build_worker_env",
    "build_worker_cmd",
    "resolve_effective_mode",
    "_tee_worker_output",
    "suppress_dedup",
    "count_main_ahead_of_epic",
    "capture_vllm_metrics",
    "capture_vllm_metrics_diagnostic",
    "compute_vllm_metrics_delta",
    "picker_sort_key",
    "WorkerBehavior",
    "WorkerTelemetry",
    "_parse_worker_behavior",
    "_parse_worker_telemetry",
    "kv_concurrency_ceiling",
    "PRODUCTION_KV_CACHE_TOKENS",
    "COMPACTION_CONTEXT_CEILING",
    "KV_BUDGET_TOKENS",
    "DEFAULT_KV_RESERVATION",
    "EFFORT_KV_RESERVATION",
    "kv_admission_ok",
]
from .watcher_types import (
    _ENV_VARS_TO_STRIP_FOR_CLOUD,
    _VLLM_BASE_URL,
    _VLLM_SERVED_MODEL,
    ActiveWorker,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KV-budget concurrency ceiling (WOR-336 spike)
# ---------------------------------------------------------------------------

# Measured vLLM KV-cache token capacity for the production server config
# (Qwen3.6-35B-A3B-NVFP4, --max-model-len 262144, --max-num-seqs 16,
# --kv-cache-dtype fp8, --gpu-memory-utilization 0.93 on a 32 GiB RTX 5090):
# the paged-KV pool holds ~173,968 tokens (~6.64 GiB). Live-measured
# 2026-05-20 (WOR-504 Phase 0) from vLLM startup log.
#
# Note: vLLM 0.20+ reserves ~3.6 pp of memory for CUDA graph profiling
# by default (effective utilization is 0.8938 at nominal 0.93). Setting
# VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=0 would reclaim ~7k more KV
# tokens but risks OOM during graph capture; deferred to a follow-up.
#
# Previous value 148,816 (at the implicit 0.90 default, pre-WOR-527) is
# documented in docs/spikes/vllm-max-num-seqs-sensitivity.md.
PRODUCTION_KV_CACHE_TOKENS = 173_968

# Observed peak per-worker input context before Claude Code compaction
# fires (CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=75 over a 240k window). Heavy
# real-worker tickets in ticket_metrics topped out at input_tokens_max
# ~134,643; one such worker single-handedly fills most of the KV pool.
COMPACTION_CONTEXT_CEILING = 134_000


# CALIBRATED CONSTANTS — transcribe verbatim.
# Source: app.db ticket_metrics.input_tokens_max n=91 + WOR-336
# empirical 2-way-safe/6-way-thrash ceiling. Do NOT re-derive.
KV_BUDGET_TOKENS = (
    133_934  # 0.9 * 148_816 (vLLM KV pool, WOR-336 prefix-survival headroom)
)
DEFAULT_KV_RESERVATION = 90_000  # unknown/None effort -> treat as xhigh (conservative)
EFFORT_KV_RESERVATION: dict[str, int] = {
    "low": 33_000,  # ~4 concurrent
    "medium": 45_000,  # ~3 concurrent
    # ~2 concurrent (= WOR-336 safe ceiling; dominant tier)
    "high": 67_000,
    "xhigh": 90_000,  # ~1-2 concurrent
    # 1 concurrent (<= budget so it still admits alone)
    "max": 130_000,
}
# admit candidate iff sum(reservation(e) for e in in_flight)
# + reservation(candidate) <= budget


def kv_concurrency_ceiling(
    per_worker_context_tokens: int,
    *,
    kv_cache_tokens: int = PRODUCTION_KV_CACHE_TOKENS,
    utilization_target: float = 0.9,
    min_workers: int = 1,
) -> int:
    """Max concurrent local workers before vLLM KV-cache oversubscription.

    Each in-flight worker holds roughly ``per_worker_context_tokens`` of
    paged-KV state (its accumulated conversation, up to the compaction
    ceiling). Once the aggregate working set exceeds the KV pool, vLLM
    evicts prefix-cache blocks between turns and every subsequent turn
    re-prefills the worker's full context. WOR-336 measured this collapse
    directly: prefix-cache hit-rate ~67% at 2-way concurrency vs ~15% at
    6-way, effective throughput ~40 -> ~4 tok/s — even though the
    controlled NVFP4 bench shows raw decode sustains ~120 tok/s per
    stream up to 8 concurrent streams (962 tok/s aggregate, VRAM-safe).
    The binding limit is KV bytes, not ``--max-num-seqs`` slots.

    Keep aggregate KV demand at or below ``utilization_target`` of
    capacity so prefix-cache blocks survive across a worker's turns.

    Args:
        per_worker_context_tokens: typical/peak active context per
            worker. Pass ``COMPACTION_CONTEXT_CEILING`` for the worst
            case.
        kv_cache_tokens: total KV-pool token capacity for the running
            vLLM config. Defaults to the measured production value.
        utilization_target: fraction of the pool to budget
            (0 < x <= 1). Headroom below 1.0 leaves room for
            prefix-cache reuse rather than 100% live-KV packing.
        min_workers: floor on the return value. A single worker always
            runs even if its context alone exceeds the pool (it just
            forfeits cross-turn prefix-cache reuse).

    Returns:
        The largest worker count whose combined KV demand stays within
        ``utilization_target * kv_cache_tokens``, never below
        ``min_workers``.

    Raises:
        ValueError: if any argument is out of range.
    """
    if kv_cache_tokens <= 0:
        raise ValueError("kv_cache_tokens must be positive")
    if per_worker_context_tokens <= 0:
        raise ValueError("per_worker_context_tokens must be positive")
    if not 0.0 < utilization_target <= 1.0:
        raise ValueError("utilization_target must be in (0, 1]")
    if min_workers < 1:
        raise ValueError("min_workers must be >= 1")

    budget = kv_cache_tokens * utilization_target
    ceiling = int(budget // per_worker_context_tokens)
    return max(min_workers, ceiling)


def kv_admission_ok(
    in_flight_efforts: Sequence[str | None],
    candidate_effort: str | None,
    *,
    budget: int = KV_BUDGET_TOKENS,
) -> bool:
    """Whether a candidate ticket fits within the KV-token budget.

    Sums the KV reservation for every in-flight worker's effort level plus
    the candidate's own reservation.  Returns ``True`` when the sum is
    within *budget*, ``False`` otherwise.

    Unknown / ``None`` effort falls back to ``DEFAULT_KV_RESERVATION``.
    """

    def _reservation(effort: str | None) -> int:
        if effort is None:
            return DEFAULT_KV_RESERVATION
        return EFFORT_KV_RESERVATION.get(effort, DEFAULT_KV_RESERVATION)

    total = sum(_reservation(e) for e in in_flight_efforts) + _reservation(
        candidate_effort
    )
    return total <= budget


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
    quality_check_budget: int | None = None,
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

    WOR-421: ``quality_check_budget`` is the per-session allowance for manual
    Bash invocations of ruff/mypy/pytest/bandit/lint-imports. Passed via
    ``WATCHER_QUALITY_CHECK_BUDGET`` env var, read by the
    ``check_quality_check_budget.py`` PreToolUse hook. Defaults to
    ``len(manifest.required_checks)`` at the call site.
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
    if quality_check_budget is not None:
        env["WATCHER_QUALITY_CHECK_BUDGET"] = str(quality_check_budget)
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


_RESOLVE_REFUSED = "refused"


def resolve_effective_mode(worker_mode: str, routing: str) -> str:
    """Return the effective execution mode based on worker_mode x routing.

    Implements the four-way (worker_mode x routing) matrix:

    +----------------+-----------+------------------+-------------+
    |                | local     | cloud_preferred  | cloud_only  |
    +----------------+-----------+------------------+-------------+
    | default        | local     | cloud            | cloud       |
    | cloud          | local     | cloud            | cloud       |
    | local          | local     | local            | refused     |
    +----------------+-----------+------------------+-------------+

    Returns ``"refused"`` when ``routing=cloud_only`` and ``worker_mode=local``.
    The caller (dispatch.start_ticket) checks for this sentinel and returns
    early — before pool capacity, worktree, or state changes.
    """
    if worker_mode == "default":
        if routing == "local":
            return "local"
        return "cloud"

    # Worker mode is an explicit override — "local" or "cloud".
    if routing == "cloud_only" and worker_mode == "local":
        return _RESOLVE_REFUSED

    if routing == "cloud_preferred" and worker_mode == "local":
        return "local"

    return worker_mode


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
_M_PREFIX_CACHE_HITS = "vllm:prefix_cache_hits_total"
_M_PREFIX_CACHE_QUERIES = "vllm:prefix_cache_queries_total"

_VLLM_COUNTERS = (
    _M_PREFIX_CACHE_HITS,
    _M_PREFIX_CACHE_QUERIES,
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:time_to_first_token_seconds_sum",
    "vllm:time_to_first_token_seconds_count",
    "vllm:num_preemptions_total",
)

# vLLM has shipped the prefix-cache counters under both the bare name and a
# ``gpu_`` prefix across engine versions (V0 vs V1). Accept either spelling
# and fold it into the canonical key so a vLLM upgrade does not silently
# blank these columns (WOR-439). Add verified spellings here as they are
# observed against a live /metrics surface.
_VLLM_COUNTER_ALIASES: dict[str, str] = {
    "vllm:gpu_prefix_cache_hits_total": _M_PREFIX_CACHE_HITS,
    "vllm:gpu_prefix_cache_queries_total": _M_PREFIX_CACHE_QUERIES,
}


def _fetch_vllm_metrics_body(timeout: float) -> str | None:
    """Fetch raw vLLM /metrics body via http.client; returns None on failure."""
    import http.client

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
            return resp.read().decode("utf-8", errors="replace")
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return None


def _parse_metric_line(line: str, targets: set[str]) -> tuple[str | None, float | None]:
    """Parse one Prometheus text line; return (name, value) or (None, None)."""
    for ch_idx, ch in enumerate(line):
        if ch == "{" or ch.isspace():
            metric_name = line[:ch_idx]
            rest = line[ch_idx:]
            break
    else:
        return None, None
    if metric_name not in targets:
        return None, None
    try:
        value = float(rest.rsplit(maxsplit=1)[-1])
    except (ValueError, IndexError):
        return None, None
    return metric_name, value


def capture_vllm_metrics_diagnostic(
    timeout: float = 5.0,
) -> tuple[dict[str, float] | None, str]:
    """Snapshot vLLM Prometheus counters, with a failure reason (WOR-439).

    Returns ``(snapshot, reason)`` where ``reason`` is one of:

    - ``"ok"`` — ``snapshot`` is a non-empty aggregated dict
    - ``"unreachable"`` — /metrics did not respond, timed out, or
      returned a non-200 status
    - ``"no_counters_matched"`` — /metrics responded but none of the
      expected counters were present (a vLLM build that renamed its
      Prometheus metrics — the silent-NULL trap WOR-439 chases)

    Counter names are matched against ``_VLLM_COUNTERS`` plus the
    version-variant spellings in ``_VLLM_COUNTER_ALIASES`` (each folded
    into its canonical key). Aggregates values across label combinations.
    """
    body = _fetch_vllm_metrics_body(timeout)
    if body is None:
        return None, "unreachable"

    canonical = set(_VLLM_COUNTERS)
    targets = canonical | set(_VLLM_COUNTER_ALIASES)
    aggregated: dict[str, float] = dict.fromkeys(canonical, 0.0)
    seen: dict[str, bool] = dict.fromkeys(canonical, False)

    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, value = _parse_metric_line(line, targets)
        if name is None or value is None:
            continue
        canon = _VLLM_COUNTER_ALIASES.get(name, name)
        aggregated[canon] += value
        seen[canon] = True

    if not any(seen.values()):
        return None, "no_counters_matched"
    return aggregated, "ok"


def capture_vllm_metrics(timeout: float = 5.0) -> dict[str, float] | None:
    """Snapshot vLLM Prometheus metrics for per-ticket attribution.

    Back-compat thin wrapper over :func:`capture_vllm_metrics_diagnostic`
    that discards the diagnostic reason. Returns the aggregated snapshot
    dict, or ``None`` if the endpoint is unreachable / malformed / not a
    vLLM /metrics surface.
    """
    snapshot, _reason = capture_vllm_metrics_diagnostic(timeout)
    return snapshot


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

    hits = _delta(_M_PREFIX_CACHE_HITS)
    queries = _delta(_M_PREFIX_CACHE_QUERIES)
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
# Same-epic pair detection (WOR-220)
# ---------------------------------------------------------------------------


def get_active_parent_ids(workers: list[ActiveWorker]) -> set[str]:
    """Return the set of parent issue IDs for all active workers.

    Each worker's manifest carries an ``epic_id`` that is the Linear parent
    issue ID of the ticket it is implementing.  This is used by the picker
    to prefer dispatching a candidate whose parent matches an already-running
    worker's parent, maximising API-cache (APC) hit rate across same-epic
    concurrent workers.
    """
    return {w.manifest.epic_id for w in workers if w.manifest.epic_id}


def picker_sort_key(
    candidate: dict[str, Any],
    active_parent_ids: set[str],
    _candidate_index: int,
) -> tuple[int, int, str]:
    """Sort key for candidate tickets at dispatch pick time.

    When there is at least one active worker (slot index > 0), candidates
    whose Linear parent matches an active worker's parent get a lower key
    so they sort first.  The full key is ``(parent_match, priority, id)``
    where ``parent_match`` is 0 when the candidate is a same-epic sibling
    and 1 otherwise — meaning same-epic tickets always sort before
    cross-epic ones, and within each group the current ordering
    (priority, then id) is preserved.

    This is a *stable* sort key: if no active workers exist, the key
    collapses to ``(1, priority, id)`` for every candidate which
    preserves the original ordering (WOR-220: solo dispatch is
    byte-for-byte unchanged).
    """
    parent_id = candidate.get("parent") or {}
    if isinstance(parent_id, dict):
        parent_id = parent_id.get("id") or ""
    else:
        parent_id = ""

    parent_match = 0 if parent_id in active_parent_ids else 1

    priority = candidate.get("priority") or 0
    if not isinstance(priority, int):
        try:
            priority = int(priority)
        except (ValueError, TypeError):
            priority = 0

    return (parent_match, priority, candidate.get("id", ""))
