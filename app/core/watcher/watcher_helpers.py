"""Pure helper functions for the watcher sub-system (no I/O, unit-testable).

All functions in this module are stateless and have no self-dependencies.
This module may import from watcher_types only (no other watcher siblings).
"""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from app.core.manifest import ExecutionManifest

from .watcher_log_parsing import (
    _HOOK_VIOLATION_TOKENS,
    _is_violation_bash_command,
    _parse_hook_trust_violations,
    _parse_worker_api_retries,
    _parse_worker_subagent_spawns,
    _parse_worker_usage,
    _process_assistant_event,
    _process_compact_boundary,
    _resolve_usage_totals,
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
    "_parse_worker_behavior",
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
# --kv-cache-dtype fp8 on a 32 GiB RTX 5090): the paged-KV pool holds
# ~148,816 tokens (~5.73 GiB). Source: WOR-336 forensic + live 6->2 A/B
# (docs/spikes/vllm-max-num-seqs-sensitivity.md).
PRODUCTION_KV_CACHE_TOKENS = 148_816

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
_VLLM_COUNTERS = (
    "vllm:prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total",
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
    "vllm:gpu_prefix_cache_hits_total": "vllm:prefix_cache_hits_total",
    "vllm:gpu_prefix_cache_queries_total": "vllm:prefix_cache_queries_total",
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
    candidate_index: int,
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
    # Usage accumulators (mirror _parse_worker_usage's locals)
    total_input = total_output = 0
    has_assistant_usage = False
    compact_count = compact_duration_ms = 0
    last_input: int | None = None
    last_output: int | None = None
    content_chars = 0

    # Counts
    subagent_spawns = 0
    api_retries = 0
    hook_violations = 0

    # Behavior accumulators (mirror _walk_behavior_log's locals)
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
                    # --- usage ---
                    i_d, o_d, has_u, cc = _process_assistant_event(obj)
                    total_input += i_d
                    total_output += o_d
                    has_assistant_usage = has_assistant_usage or has_u
                    content_chars += cc

                    # --- behavior ---
                    turn_count += 1
                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    (
                        input_tokens_first,
                        input_tokens_last,
                        input_tokens_max,
                    ) = _update_input_tokens(
                        usage.get("input_tokens"),
                        input_tokens_first,
                        input_tokens_last,
                        input_tokens_max,
                    )

                    # --- behavior content + spawns + hook violations ---
                    for blk in msg.get("content") or []:
                        if not isinstance(blk, dict):
                            continue
                        _accumulate_content_block(
                            blk, behavior_accum, tool_breakdown, read_counts
                        )
                        if blk.get("type") == "tool_use":
                            name = blk.get("name")
                            if name == "Task":
                                subagent_spawns += 1
                            elif name == "Bash":
                                token = _is_violation_bash_command(blk)
                                if token and token in _HOOK_VIOLATION_TOKENS:
                                    hook_violations += 1
                elif obj_type == "system":
                    subtype = obj.get("subtype")
                    if subtype == "compact_boundary":
                        c_d, d_d = _process_compact_boundary(obj)
                        compact_count += c_d
                        compact_duration_ms += d_d
                    elif subtype == "api_retry":
                        api_retries += 1
                elif obj_type == "result":
                    usage = obj.get("usage") or {}
                    last_input = usage.get("input_tokens")
                    last_output = usage.get("output_tokens")
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
        total_input,
        total_output,
        has_assistant_usage,
        compact_count,
        compact_duration_ms,
        last_input,
        last_output,
        content_chars,
    )

    if turn_count == 0:
        behavior = WorkerBehavior.empty_readable()
    else:
        redundant = sum(1 for n in read_counts.values() if n > 2)
        behavior = WorkerBehavior(
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

    return WorkerTelemetry(
        input_tokens=input_tok,
        output_tokens=output_tok,
        context_compactions=compactions,
        compact_duration_ms=compact_dur,
        subagent_spawns=subagent_spawns,
        api_retries=api_retries,
        hook_trust_violations=hook_violations,
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
