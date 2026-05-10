"""Per-ticket cost and execution metrics store.

SQLite-backed store for tracking local vs. cloud usage per ticket.
The watcher is the sole writer; workers emit JSON result files only.
The DB is shared across projects via a project_id column for cross-epic analysis.
"""

from __future__ import annotations

import json
import platform
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator, Literal

from pydantic import BaseModel, Field

ImplementationMode = Literal["local", "cloud", "hybrid"]
Outcome = Literal["success", "failure", "escalated", "aborted"]
CheckOutcome = Literal["passed", "failed"]

_APP_DIR = "repo-scaffold"
_DB_NAME = "app.db"

_CREATE_CHECK_RUN_LOG = """
CREATE TABLE IF NOT EXISTS check_run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    check_cmd   TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    duration_s  REAL,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_RUN_LOG = """
CREATE TABLE IF NOT EXISTS ticket_run_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id       TEXT NOT NULL,
    attempt         INTEGER NOT NULL,
    implementation_mode TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    failed_check    TEXT,
    wall_time_s     REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    output_tok_per_s REAL,
    context_compactions INTEGER,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS ticket_metrics (
    ticket_id             TEXT NOT NULL,
    project_id            TEXT NOT NULL,
    epic_id               TEXT,
    implementation_mode   TEXT NOT NULL,
    cloud_used            INTEGER NOT NULL DEFAULT 0,
    cloud_model           TEXT,
    cloud_tokens          INTEGER,
    cloud_cost_estimate   REAL,
    local_used            INTEGER NOT NULL DEFAULT 0,
    local_model           TEXT,
    local_input_tokens    INTEGER,
    local_output_tokens   INTEGER,
    local_tokens          INTEGER,
    local_wall_time       REAL,
    output_tokens_per_wall_second REAL,
    escalated_to_cloud    INTEGER NOT NULL DEFAULT 0,
    outcome               TEXT NOT NULL,
    retry_count           INTEGER NOT NULL DEFAULT 0,
    check_failures_json   TEXT,
    lines_changed         INTEGER,
    files_changed         INTEGER,
    sonar_findings_count  INTEGER,
    context_compactions   INTEGER,
    change_type           TEXT,
    reasoning_demand      INTEGER,
    scope_clarity         INTEGER,
    constraint_density    INTEGER,
    ac_specificity        INTEGER,
    tech_stack            TEXT,
    raw_extensions        TEXT,
    waste_score           INTEGER,
    waste_breakdown_json  TEXT,
    tags                  TEXT,
    notes                 TEXT,
    effort                TEXT,
    compact_duration_ms   INTEGER,
    api_retry_count       INTEGER,
    subagent_spawns       INTEGER,
    dispatch_concurrency  INTEGER,
    recorded_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticket_id, project_id)
)
"""


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
    attempt: int = Field(description="1-based attempt number (retry_count + 1)")
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


# Pre-built cost-rollup SQL strings keyed by period. Defined as module-level
# constants rather than f-string-interpolated at call time so that semgrep can
# verify there is no SQL composition with variable input. The trailing WHERE
# clauses are the only thing that varies per period.
_COST_ROLLUP_SQL_BASE = """
SELECT
    COALESCE(SUM(CASE WHEN cloud_used = 1
            THEN cloud_cost_estimate ELSE 0 END), 0)
        AS cloud_spent,
    COALESCE(SUM(CASE WHEN cloud_used = 1 THEN 1 ELSE 0 END), 0)
        AS cloud_ticket_count,
    COALESCE(SUM(CASE WHEN local_used = 1
            THEN local_input_tokens * 3.0 / 1e6
                 + local_output_tokens * 15.0 / 1e6
            ELSE 0 END), 0)
        AS local_saved,
    COALESCE(SUM(CASE WHEN local_used = 1 THEN 1 ELSE 0 END), 0)
        AS local_ticket_count
FROM ticket_metrics
"""
_COST_ROLLUP_SQL_TODAY = (
    _COST_ROLLUP_SQL_BASE + "WHERE recorded_at >= date('now', 'start of day')"
)
_COST_ROLLUP_SQL_WEEK = (
    _COST_ROLLUP_SQL_BASE + "WHERE recorded_at >= date('now', '-7 days')"
)
_COST_ROLLUP_SQL_ALL = _COST_ROLLUP_SQL_BASE


class MetricsStore:
    """SQLite-backed store for ticket execution metrics."""

    _APP_DIR = _APP_DIR

    @classmethod
    def get_db_path(cls) -> Path:
        if platform.system() == "Windows":
            base = Path.home() / "AppData" / "Roaming"
        else:
            base = Path.home() / ".config"
        return base / cls._APP_DIR / _DB_NAME

    def __init__(self, db_path: Path | None = None) -> None:
        self._path = db_path if db_path is not None else self.get_db_path()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_CHECK_RUN_LOG)
            conn.execute(_CREATE_RUN_LOG)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add new columns to existing databases using PRAGMA table_info."""
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(ticket_metrics)").fetchall()
        }
        if "local_input_tokens" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN local_input_tokens INTEGER"
            )
        if "local_output_tokens" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN local_output_tokens INTEGER"
            )
        # WOR-361: rename misleading column. Idempotent across states:
        # - fresh DB: CREATE TABLE already used the new name
        # - already-migrated: new name exists, skip
        # - pre-rename DB: rename old → new
        # - very old DB without either: ADD COLUMN with new name
        if "output_tokens_per_wall_second" not in existing:
            if "local_output_tokens_per_second" in existing:
                conn.execute(
                    "ALTER TABLE ticket_metrics RENAME COLUMN "
                    "local_output_tokens_per_second TO output_tokens_per_wall_second"
                )
            else:
                conn.execute(
                    "ALTER TABLE ticket_metrics "
                    "ADD COLUMN output_tokens_per_wall_second REAL"
                )
        # WOR-262 taxonomy columns
        if "change_type" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN change_type TEXT")
        if "reasoning_demand" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN reasoning_demand INTEGER"
            )
        if "scope_clarity" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN scope_clarity INTEGER")
        if "constraint_density" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN constraint_density INTEGER"
            )
        if "ac_specificity" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN ac_specificity INTEGER")
        if "tech_stack" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN tech_stack TEXT")
        if "raw_extensions" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN raw_extensions TEXT")
        if "waste_score" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN waste_score INTEGER")
        if "waste_breakdown_json" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN waste_breakdown_json TEXT"
            )
        if "tags" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN tags TEXT")
        if "notes" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN notes TEXT")
        if "effort" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN effort TEXT")
        if "compact_duration_ms" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN compact_duration_ms INTEGER"
            )
        if "api_retry_count" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN api_retry_count INTEGER"
            )
        if "subagent_spawns" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN subagent_spawns INTEGER"
            )
        if "hook_trust_violations" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN hook_trust_violations INTEGER"
            )
        if "dispatch_concurrency" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN dispatch_concurrency INTEGER"
            )
        # WOR-370: vLLM /metrics delta capture (only populated when
        # dispatch_concurrency==0 at dispatch AND no peer was launched
        # during the session — otherwise the server-wide counters mix
        # multiple workers' traffic and per-ticket attribution breaks).
        if "vllm_metrics_attributable" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics "
                "ADD COLUMN vllm_metrics_attributable INTEGER"
            )
        if "vllm_prefix_cache_hits" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_prefix_cache_hits INTEGER"
            )
        if "vllm_prefix_cache_queries" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics "
                "ADD COLUMN vllm_prefix_cache_queries INTEGER"
            )
        if "vllm_prefix_cache_hit_ratio" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_prefix_cache_hit_ratio REAL"
            )
        if "vllm_prompt_tokens" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_prompt_tokens INTEGER"
            )
        if "vllm_generation_tokens" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_generation_tokens INTEGER"
            )
        if "vllm_ttft_seconds_sum" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_ttft_seconds_sum REAL"
            )
        if "vllm_ttft_count" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_ttft_count INTEGER"
            )
        if "vllm_ttft_mean_seconds" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_ttft_mean_seconds REAL"
            )
        if "vllm_preemptions" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN vllm_preemptions INTEGER"
            )
        # WOR-380: per-worker behavior telemetry from stream-json log.
        # Concurrency-safe sibling to WOR-370 — all derived from the
        # worker's own log file.
        if "turn_count" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN turn_count INTEGER")
        if "tool_calls_total" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN tool_calls_total INTEGER"
            )
        if "tool_calls_breakdown" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN tool_calls_breakdown TEXT"
            )
        if "thinking_blocks" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN thinking_blocks INTEGER"
            )
        if "thinking_chars_total" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN thinking_chars_total INTEGER"
            )
        if "input_tokens_max" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN input_tokens_max INTEGER"
            )
        if "input_tokens_first" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN input_tokens_first INTEGER"
            )
        if "input_tokens_last" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN input_tokens_last INTEGER"
            )
        if "redundant_reads_count" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN redundant_reads_count INTEGER"
            )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, metrics: TicketMetrics) -> None:
        """Upsert a ticket metrics record (ticket_id + project_id is the PK)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ticket_metrics (
                    ticket_id, project_id, epic_id, implementation_mode,
                    cloud_used, cloud_model, cloud_tokens, cloud_cost_estimate,
                    local_used, local_model, local_input_tokens, local_output_tokens,
                    local_tokens, local_wall_time, output_tokens_per_wall_second,
                    escalated_to_cloud, outcome,
                    retry_count, check_failures_json,
                    lines_changed, files_changed,
                    sonar_findings_count, context_compactions,
                    change_type, reasoning_demand, scope_clarity,
                    constraint_density, ac_specificity, tech_stack, raw_extensions,
                    waste_score, waste_breakdown_json, tags, notes, effort,
                    compact_duration_ms, api_retry_count, subagent_spawns,
                    hook_trust_violations, dispatch_concurrency
                ) VALUES (
                    :ticket_id, :project_id, :epic_id, :implementation_mode,
                    :cloud_used, :cloud_model, :cloud_tokens, :cloud_cost_estimate,
                    :local_used, :local_model,
                    :local_input_tokens, :local_output_tokens,
                    :local_tokens, :local_wall_time,
                    :output_tokens_per_wall_second,
                    :escalated_to_cloud, :outcome,
                    :retry_count, :check_failures_json,
                    :lines_changed, :files_changed,
                    :sonar_findings_count, :context_compactions,
                    :change_type, :reasoning_demand, :scope_clarity,
                    :constraint_density, :ac_specificity, :tech_stack, :raw_extensions,
                    :waste_score, :waste_breakdown_json, :tags, :notes, :effort,
                    :compact_duration_ms, :api_retry_count, :subagent_spawns,
                    :hook_trust_violations, :dispatch_concurrency
                )
                """,
                {
                    **metrics.model_dump(exclude={"check_failures"}),
                    "cloud_used": int(metrics.cloud_used),
                    "local_used": int(metrics.local_used),
                    "escalated_to_cloud": int(metrics.escalated_to_cloud),
                    "check_failures_json": (
                        json.dumps(metrics.check_failures)
                        if metrics.check_failures is not None
                        else None
                    ),
                },
            )

    def get_by_ticket(self, ticket_id: str, project_id: str) -> TicketMetrics | None:
        """Return the metrics record for a ticket, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ticket_metrics WHERE ticket_id = ? AND project_id = ?",
                (ticket_id, project_id),
            ).fetchone()
        if row is None:
            return None
        return _row_to_metrics(row)

    def set_tags(self, ticket_id: str, project_id: str, tags: list[str] | None) -> None:
        """Set the auto-detected tags for a ticket (overwrite existing)."""
        tags_json = json.dumps(tags) if tags else None
        with self._connect() as conn:
            conn.execute(
                "UPDATE ticket_metrics SET tags = ? "
                "WHERE ticket_id = ? AND project_id = ?",
                (tags_json, ticket_id, project_id),
            )

    def get_by_epic(self, epic_id: str, project_id: str) -> list[TicketMetrics]:
        """Return all ticket metrics for an epic."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ticket_metrics WHERE epic_id = ? AND project_id = ?",
                (epic_id, project_id),
            ).fetchall()
        return [_row_to_metrics(r) for r in rows]

    def epic_summary(self, epic_id: str, project_id: str) -> EpicSummary:
        """Return aggregated totals for all tickets in an epic."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*)                              AS ticket_count,
                    COALESCE(SUM(cloud_tokens), 0)        AS cloud_tokens_total,
                    COALESCE(SUM(cloud_cost_estimate), 0) AS cloud_cost_total,
                    COALESCE(SUM(local_tokens), 0)        AS local_tokens_total,
                    COALESCE(SUM(local_wall_time), 0)     AS local_wall_time_total,
                    COALESCE(SUM(escalated_to_cloud), 0)  AS escalation_count,
                    COALESCE(SUM(retry_count), 0)         AS retry_count_total,
                    COALESCE(SUM(lines_changed), 0)       AS lines_changed_total,
                    COALESCE(SUM(files_changed), 0)       AS files_changed_total,
                    COALESCE(SUM(sonar_findings_count), 0) AS sonar_findings_total
                FROM ticket_metrics
                WHERE epic_id = ? AND project_id = ?
                """,
                (epic_id, project_id),
            ).fetchone()
        return EpicSummary(
            epic_id=epic_id,
            project_id=project_id,
            ticket_count=row["ticket_count"],
            cloud_tokens_total=row["cloud_tokens_total"],
            cloud_cost_total=row["cloud_cost_total"],
            local_tokens_total=row["local_tokens_total"],
            local_wall_time_total=row["local_wall_time_total"],
            escalation_count=row["escalation_count"],
            retry_count_total=row["retry_count_total"],
            lines_changed_total=row["lines_changed_total"],
            files_changed_total=row["files_changed_total"],
            sonar_findings_total=row["sonar_findings_total"],
        )

    def get_cost_rollup(self, period: Literal["today", "week", "all"]) -> CostRollup:
        """Return aggregated cost economics for *period*.

        *today*   = rows where ``recorded_at >= date('now', 'start of day')``
        *week*    = rows where ``recorded_at >= date('now', '-7 days')``
        *all*     = no filter

        cloud_spent  = SUM(cloud_cost_estimate) where cloud_used=1
        local_saved  = SUM(local_input_tokens * input_rate + local_output_tokens
                         * output_rate) where local_used=1; sonnet-4-6 pricing
        """
        # Pre-built SQL strings keyed by period — no f-string interpolation,
        # no user input ever touches these queries (period is a Literal type
        # constrained to the dict keys at the call site).
        queries = {
            "today": _COST_ROLLUP_SQL_TODAY,
            "week": _COST_ROLLUP_SQL_WEEK,
            "all": _COST_ROLLUP_SQL_ALL,
        }
        with self._connect() as conn:
            row = conn.execute(queries[period]).fetchone()
        return CostRollup(
            cloud_spent=row["cloud_spent"],
            local_saved=row["local_saved"],
            cloud_ticket_count=row["cloud_ticket_count"],
            local_ticket_count=row["local_ticket_count"],
        )

    def record_run(self, entry: TicketRunLog) -> None:
        """Append a single run-log row per attempt (append-only, not upsert)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ticket_run_log
                    (ticket_id, attempt, implementation_mode, outcome,
                     failed_check, wall_time_s, input_tokens, output_tokens,
                     output_tok_per_s, context_compactions)
                VALUES
                    (:ticket_id, :attempt, :implementation_mode, :outcome,
                     :failed_check, :wall_time_s, :input_tokens, :output_tokens,
                     :output_tok_per_s, :context_compactions)
                """,
                entry.model_dump(),
            )

    def record_check_run(self, entry: CheckRunEntry) -> None:
        """Append a single check execution row to check_run_log."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO check_run_log
                    (ticket_id, project_id, check_cmd, outcome, duration_s)
                VALUES
                    (:ticket_id, :project_id, :check_cmd, :outcome, :duration_s)
                """,
                entry.model_dump(),
            )

    def get_check_stats(self, project_id: str) -> list[CheckStats]:
        """Aggregated pass/fail and timing stats per check command, slowest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    check_cmd,
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN outcome = 'passed' THEN 1 ELSE 0 END) AS pass_count,
                    SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END) AS fail_count,
                    ROUND(
                        100.0
                        * SUM(CASE WHEN outcome = 'passed' THEN 1 ELSE 0 END)
                        / COUNT(*),
                        1
                    ) AS pass_pct,
                    AVG(duration_s) AS avg_duration_s,
                    MAX(duration_s) AS max_duration_s
                FROM check_run_log
                WHERE project_id = ?
                GROUP BY check_cmd
                ORDER BY avg_duration_s DESC NULLS LAST
                """,
                (project_id,),
            ).fetchall()
        return [
            CheckStats(
                check_cmd=r["check_cmd"],
                total_runs=r["total_runs"],
                pass_count=r["pass_count"],
                fail_count=r["fail_count"],
                pass_pct=r["pass_pct"] if r["pass_pct"] is not None else 0.0,
                avg_duration_s=r["avg_duration_s"],
                max_duration_s=r["max_duration_s"],
            )
            for r in rows
        ]


def compute_tags(
    ticket_metrics_row: TicketMetrics,
    result_json_status: str,
    result_json_flags: dict[str, bool] | None = None,
    tracked_prs: list[Any] | None = None,
) -> list[str]:
    """Return auto-detected tags for a ticket metrics row.

    Pure function — no I/O, no logging, no side effects.  Easy to unit-test.

    Four anomaly-detection rules (from the 2026-05-03 retro) and five
    categorization rules (from existing result.json signals).

    Args:
        ticket_metrics_row: The TicketMetrics record as written by finalize_worker.
        result_json_status: The ``status`` field from the worker's result.json.
        result_json_flags: Parsed flags dict from result.json (scope_drift, etc.).
        tracked_prs: List of TrackedPR objects that were populated during PR creation.

    Returns:
        A list of tag name strings.  May be empty.
    """
    tags: list[str] = []
    outcome = ticket_metrics_row.outcome
    lines_changed = ticket_metrics_row.lines_changed
    waste_score = ticket_metrics_row.waste_score
    retry_count = ticket_metrics_row.retry_count
    local_tokens = ticket_metrics_row.local_tokens
    local_wall_time = ticket_metrics_row.local_wall_time
    api_retry_count = ticket_metrics_row.api_retry_count
    context_compactions = ticket_metrics_row.context_compactions

    # --- Anomaly detection rules (4) ---
    # Type-strict guards (isinstance vs `is not None`) so the function is
    # robust to non-numeric inputs (e.g. MagicMock leaking through tests that
    # mock `metrics.get_by_ticket`). Without this guard, comparisons against
    # non-numeric values raise TypeError and break unrelated tests.

    # zero_tokens_high_wall_time: local worker burned wall time with almost no
    # tokens — likely a failed run that still consumed time (2026-05-03 retro).
    if isinstance(local_tokens, (int, float)) and isinstance(
        local_wall_time, (int, float)
    ):
        if local_tokens < 100_000 and local_wall_time > 1_800:
            tags.append("zero_tokens_high_wall_time")

    # no_diff_against_base: the worker reported failure but produced no diff —
    # it never got far enough to write code (2026-05-03 retro).
    if outcome == "failure" and isinstance(lines_changed, int) and lines_changed == 0:
        tags.append("no_diff_against_base")

    # success_outcome_state_mismatch: result.json says success but metrics
    # record captured failure — state machine inconsistency (2026-05-03 retro).
    if result_json_status == "success" and outcome == "failure":
        tags.append("success_outcome_state_mismatch")

    # success_pr_create_failed: the worker succeeded but the PR push failed —
    # the PR is not visible in the repo (2026-05-03 retro).
    if outcome == "success" and tracked_prs is not None and len(tracked_prs) == 0:
        tags.append("success_pr_create_failed")

    # --- Categorization rules (4) ---

    # scope_drift: the worker itself flagged that it went beyond scope.
    if result_json_flags and result_json_flags.get("scope_drift"):
        tags.append("scope_drift")

    # escalated: the final outcome was an escalation to cloud.
    if outcome == "escalated":
        tags.append("escalated")

    # high_waste: the waste score exceeds the 80 threshold.
    if isinstance(waste_score, (int, float)) and waste_score > 80:
        tags.append("high_waste")

    # rework: the ticket required at least one retry.
    if isinstance(retry_count, int) and retry_count > 0:
        tags.append("rework")

    # mid_session_compaction: the session performed at least one context
    # compaction — the LLM context was truncated during the run.
    if isinstance(context_compactions, int) and context_compactions > 0:
        tags.append("mid_session_compaction")

    # backend_unstable: high count of Claude Code internal api_retry events.
    # Threshold 6 calibrated on 2026-05-04 backfill — Pearson r=0.665 vs
    # wall_time, with the 6+ bucket running 4× slower than the no-retry
    # baseline (28 min vs 121 min mean). WOR-366.
    if isinstance(api_retry_count, int) and api_retry_count >= 6:
        tags.append("backend_unstable")

    return tags


def _row_to_metrics(row: sqlite3.Row) -> TicketMetrics:
    d = dict(row)
    d["cloud_used"] = bool(d["cloud_used"])
    d["local_used"] = bool(d["local_used"])
    d["escalated_to_cloud"] = bool(d["escalated_to_cloud"])
    raw_failures = d.pop("check_failures_json", None)
    d["check_failures"] = json.loads(raw_failures) if raw_failures is not None else None
    d.pop("recorded_at", None)
    return TicketMetrics.model_validate(d)
