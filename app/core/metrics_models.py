"""Pydantic row models and read-side aggregates for the metrics store.

Pure data — no SQLite, no I/O. Safe to import from any caller (reporting,
analytics) without dragging in the store dependency. The matching write
path lives in app.core.metrics_store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

ImplementationMode = Literal["local", "cloud", "hybrid"]
Outcome = Literal["success", "failure", "escalated", "aborted"]
CheckOutcome = Literal["passed", "failed"]


class TicketMetrics(BaseModel):
    """Metrics record for a single ticket execution."""

    model_config = {"extra": "forbid"}

    ticket_id: str
    project_id: str
    epic_id: str | None = None
    implementation_mode: ImplementationMode
    cloud_used: bool = False
    cloud_model: str | None = None
    cloud_tokens: int | None = None
    cloud_cost_estimate: float | None = None
    local_used: bool = False
    local_model: str | None = None
    local_input_tokens: int | None = None
    local_output_tokens: int | None = None
    local_tokens: int | None = None
    output_tokens_per_wall_second: float | None = Field(
        default=None,
        description=(
            "output_tokens / total local_wall_time. WOR-361: this is NOT decode "
            "throughput — wall_time includes prefill, tool exec, hooks, and "
            "compaction. For real decode rate, query vLLM /metrics directly. "
            "Useful only for 'did this ticket take a long time relative to its "
            "output' style questions."
        ),
    )
    local_wall_time: float | None = Field(default=None, description="Seconds")
    escalated_to_cloud: bool = False
    outcome: Outcome
    retry_count: int = 0
    check_failures: list[dict[str, int | str]] | None = Field(
        default=None,
        description="Per-check failures from run_checks, e.g. "
        "[{'check': 'mypy', 'exit_code': 1}]",
    )
    lines_changed: int | None = Field(
        default=None, description="Lines added + removed in the PR diff"
    )
    files_changed: int | None = Field(
        default=None, description="Number of files touched in the PR diff"
    )
    sonar_findings_count: int | None = Field(
        default=None, description="SonarCloud finding count on the resulting PR"
    )
    context_compactions: int | None = Field(
        default=None,
        description="Claude Code context compaction count during the session",
    )
    # Ticket taxonomy fields (WOR-262) — populated at /start-ticket plan time and
    # copied into the metrics row by finalize_worker. All Optional; no ticket is
    # ever blocked by missing taxonomy.
    change_type: str | None = Field(
        default=None,
        description="One of: additive, modification, refactor, removal, docs",
    )
    reasoning_demand: int | None = Field(
        default=None,
        description="1-5: depth of cross-file reasoning needed",
    )
    scope_clarity: int | None = Field(
        default=None,
        description="1-5: how explicit the acceptance criteria are",
    )
    constraint_density: int | None = Field(
        default=None,
        description="1-5: number of hard rules in the manifest",
    )
    ac_specificity: int | None = Field(
        default=None,
        description="1-5: how testable the acceptance criteria are",
    )
    tech_stack: str | None = Field(
        default=None,
        description="Comma-separated tags, e.g. 'python,sqlite,pydantic'",
    )
    raw_extensions: str | None = Field(
        default=None,
        description="JSON array string of file extensions touched",
    )
    waste_score: int | None = Field(
        default=None,
        description="0-100 waste score for the worker session",
    )
    waste_breakdown_json: str | None = Field(
        default=None,
        description="JSON string of per-signal breakdown for dashboard drill-down",
    )
    tags: str | None = Field(
        default=None,
        description="JSON array string of auto-detected tags, "
        'e.g. "[\\"zero_tokens_high_wall_time\\",\\"high_waste\\"]"',
    )
    notes: str | None = Field(
        default=None,
        description="Free-form operator notes for morning retros",
    )
    effort: str | None = Field(
        default=None,
        description="Effort level from the manifest: low/medium/high/xhigh/max",
    )
    compact_duration_ms: int | None = Field(
        default=None,
        description=(
            "Cumulative ms spent on context compaction during the session "
            "(WOR-358). Sum of compact_metadata.duration_ms across all "
            "compact_boundary system events."
        ),
    )
    api_retry_count: int | None = Field(
        default=None,
        description=(
            "Count of system/api_retry events (WOR-360) — Claude Code's "
            "transient backend retries. Backend-stability proxy."
        ),
    )
    subagent_spawns: int | None = Field(
        default=None,
        description=(
            "Count of Task-tool invocations (WOR-364). Each spawns a subagent "
            "with its own LLM stream — multiplies effective vLLM concurrency."
        ),
    )
    hook_trust_violations: int | None = Field(
        default=None,
        description=(
            "Count of manual quality-check Bash invocations during the worker "
            "session (WOR-274). Values above 1 indicate the worker ran "
            "ruff/mypy/pytest/bandit/lint-imports outside PostToolUse hooks."
        ),
    )
    dispatch_concurrency: int | None = Field(
        default=None,
        description=(
            "Count of OTHER active workers (local + cloud) at the moment this "
            "worker launched (WOR-363). 0 = solo dispatch. Empirical input for "
            "throughput-vs-concurrency analysis."
        ),
    )
    # WOR-370: vLLM /metrics deltas captured during this session. Only
    # populated when dispatch_concurrency==0 at dispatch AND no peer was
    # launched during the session. attribute=False sessions leave all fields
    # below as None.
    vllm_metrics_attributable: bool | None = Field(
        default=None,
        description=(
            "True iff the worker was solo throughout its session and the "
            "vLLM /metrics deltas below are attributable to this ticket. "
            "False if a peer was dispatched during the session. None if "
            "the snapshot was never captured (e.g. /metrics unreachable)."
        ),
    )
    vllm_prefix_cache_hits: int | None = Field(
        default=None, description="Delta of vllm:prefix_cache_hits_total"
    )
    vllm_prefix_cache_queries: int | None = Field(
        default=None, description="Delta of vllm:prefix_cache_queries_total"
    )
    vllm_prefix_cache_hit_ratio: float | None = Field(
        default=None,
        description="Derived: hits/queries during the session, range 0-1",
    )
    vllm_prompt_tokens: int | None = Field(
        default=None, description="Delta of vllm:prompt_tokens_total"
    )
    vllm_generation_tokens: int | None = Field(
        default=None, description="Delta of vllm:generation_tokens_total"
    )
    vllm_ttft_seconds_sum: float | None = Field(
        default=None,
        description="Delta of vllm:time_to_first_token_seconds_sum",
    )
    vllm_ttft_count: int | None = Field(
        default=None,
        description="Delta of vllm:time_to_first_token_seconds_count",
    )
    vllm_ttft_mean_seconds: float | None = Field(
        default=None,
        description="Derived: ttft_seconds_sum / ttft_count for the session",
    )
    vllm_preemptions: int | None = Field(
        default=None,
        description=(
            "Delta of vllm:num_preemptions_total during the session. >0 "
            "indicates KV cache pressure forced request preemption."
        ),
    )
    # WOR-380: per-worker behavior telemetry from stream-json log.
    # Concurrency-safe — derived from the worker's own log file.
    turn_count: int | None = Field(
        default=None,
        description="Number of assistant messages (turns) in the session.",
    )
    tool_calls_total: int | None = Field(
        default=None, description="Total tool_use blocks emitted during the session."
    )
    tool_calls_breakdown: str | None = Field(
        default=None,
        description=(
            "JSON-serialized dict of tool_use counts by tool name, e.g. "
            '\'{"Read": 7, "Edit": 6, "Bash": 10}\'.'
        ),
    )
    thinking_blocks: int | None = Field(
        default=None,
        description="Count of `type=thinking` content blocks (Qwen3 reasoning).",
    )
    thinking_chars_total: int | None = Field(
        default=None,
        description="Sum of len(text) across all thinking blocks (reasoning depth).",
    )
    input_tokens_max: int | None = Field(
        default=None,
        description="Max input_tokens across turns (context-window pressure proxy).",
    )
    input_tokens_first: int | None = Field(
        default=None, description="input_tokens of the first assistant turn."
    )
    input_tokens_last: int | None = Field(
        default=None, description="input_tokens of the last assistant turn."
    )
    redundant_reads_count: int | None = Field(
        default=None,
        description=(
            "Number of distinct file paths read more than 2 times in the "
            "session (WOR-355 cap). With WOR-371's hook live this should "
            "trend to 0; useful retro signal for sessions before the hook "
            "or for cap-exceeded violations that slipped through."
        ),
    )
    billing_bucket: str | None = Field(
        default=None,
        description=(
            "Billing bucket for 2026-06-15 policy split: 'local', 'subscription', "
            "'agent_sdk_credit', or 'unknown' (legacy rows without a value)."
        ),
    )


class EpicSummary(BaseModel):
    """Aggregated metrics for all tickets in an epic."""

    model_config = {"extra": "forbid"}

    epic_id: str
    project_id: str
    ticket_count: int
    cloud_tokens_total: int
    cloud_cost_total: float
    local_tokens_total: int
    local_wall_time_total: float
    escalation_count: int
    retry_count_total: int
    lines_changed_total: int
    files_changed_total: int
    sonar_findings_total: int


class TicketRunLog(BaseModel):
    """Per-attempt run log for a single ticket execution."""

    model_config = {"extra": "forbid"}

    ticket_id: str
    attempt: int = Field(
        description="1-based total attempt count (0-indexed attempt_count + 1)"
    )
    implementation_mode: ImplementationMode
    outcome: Outcome
    failed_check: str | None = None
    wall_time_s: float | None = Field(default=None, description="Wall time in seconds")
    input_tokens: int | None = None
    output_tokens: int | None = None
    output_tok_per_s: float | None = Field(
        default=None, description="output_tokens / wall_time when both present"
    )
    context_compactions: int | None = Field(
        default=None,
        description="Claude Code context compaction count during the session",
    )
    same_epic_pair: bool = Field(
        default=False,
        description=(
            "True when this worker's parent matches any other active worker's "
            "parent at dispatch time"
        ),
    )


class CheckRunEntry(BaseModel):
    """A single execution of one required_check command."""

    model_config = {"extra": "forbid"}

    ticket_id: str
    project_id: str
    check_cmd: str
    outcome: CheckOutcome
    duration_s: float | None = Field(default=None, description="Wall time in seconds")


class CheckStats(BaseModel):
    """Aggregated pass/fail and timing stats for one check command."""

    model_config = {"extra": "forbid"}

    check_cmd: str
    total_runs: int
    pass_count: int
    fail_count: int
    pass_pct: float = Field(description="0–100")
    avg_duration_s: float | None
    max_duration_s: float | None


@dataclass
class CostRollup:
    """Aggregated cost economics over a time window."""

    cloud_spent: float = 0.0
    local_saved: float = 0.0
    cloud_ticket_count: int = 0
    local_ticket_count: int = 0


@dataclass
class RoutingDistribution:
    """Routing distribution and savings breakdown.

    *local_preferred* = ``billing_bucket='local'`` — tickets that cost-economics
    consider local-first (most sub-ticket implementations).
    *cloud_preferred* = ``billing_bucket='subscription'`` — tickets that are
    inherently cloud (e.g. Linear API calls, CI).  *cloud_only* = rows where
    ``billing_bucket IS NULL`` — legacy/unknown bucket, assumed cloud.

    ``cloud_preferred_local_ran`` counts cloud_preferred tickets whose
    ``implementation_mode`` is ``local`` — cloud-preferred but ran on local
    hardware (e.g. Linear API call fallback).

    ``total_savings`` is the computed savings from
    ``cloud_preferred_local_ran`` tickets: their local_saved minus their
    cloud_cost_estimate (positive means local ran cheaper than cloud would
    have).
    """

    local_preferred_count: int = 0
    cloud_preferred_count: int = 0
    cloud_only_count: int = 0
    cloud_preferred_local_ran: int = 0
    cloud_preferred_cloud_ran: int = 0
    local_preferred_local_ran: int = 0
    local_preferred_cloud_ran: int = 0
    total_local_saved: float = 0.0
    total_cloud_cost: float = 0.0
    total_savings: float = 0.0
