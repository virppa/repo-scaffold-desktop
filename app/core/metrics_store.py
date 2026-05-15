"""SQLite-backed MetricsStore + schema migrations.

The watcher is the sole writer; workers emit JSON result files only.
Row models and tag rules live in sibling modules (metrics_models,
metrics_tags) so this file can stay focused on the I/O surface.
"""

from __future__ import annotations

import json
import platform
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Literal, overload

from app.core.metrics_models import (
    CheckRunEntry,
    CheckStats,
    CostRollup,
    EpicSummary,
    TicketMetrics,
    TicketRunLog,
)

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
    same_epic_pair  INTEGER NOT NULL DEFAULT 0,
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
WHERE billing_bucket IS NULL
   OR (billing_bucket = 'local' OR billing_bucket = 'subscription'
       OR billing_bucket = 'agent_sdk_credit')
"""
_COST_ROLLUP_SQL_TODAY = (
    _COST_ROLLUP_SQL_BASE + "AND recorded_at >= date('now', 'start of day')"
)
_COST_ROLLUP_SQL_WEEK = (
    _COST_ROLLUP_SQL_BASE + "AND recorded_at >= date('now', '-7 days')"
)
_COST_ROLLUP_SQL_ALL = _COST_ROLLUP_SQL_BASE


# Grouped queries for by_bucket=True: GROUP BY billing_bucket.
_COST_ROLLUP_SQL_ALL_GROUPED = (
    "SELECT billing_bucket,"
    "        COALESCE(SUM(CASE WHEN cloud_used = 1"
    "                THEN cloud_cost_estimate ELSE 0 END), 0)"
    "            AS cloud_spent,"
    "        COALESCE(SUM(CASE WHEN cloud_used = 1 THEN 1 ELSE 0 END), 0)"
    "            AS cloud_ticket_count,"
    "        COALESCE(SUM(CASE WHEN local_used = 1"
    "                THEN local_input_tokens * 3.0 / 1e6"
    "                     + local_output_tokens * 15.0 / 1e6"
    "                ELSE 0 END), 0)"
    "            AS local_saved,"
    "        COALESCE(SUM(CASE WHEN local_used = 1 THEN 1 ELSE 0 END), 0)"
    "            AS local_ticket_count"
    "    FROM ticket_metrics"
    "    WHERE billing_bucket IS NOT NULL"
    "      AND (billing_bucket = 'local' OR billing_bucket = 'subscription'"
    "           OR billing_bucket = 'agent_sdk_credit')"
    "    GROUP BY billing_bucket"
)
_COST_ROLLUP_SQL_TODAY_GROUPED = (
    "SELECT billing_bucket,"
    "        COALESCE(SUM(CASE WHEN cloud_used = 1"
    "                THEN cloud_cost_estimate ELSE 0 END), 0)"
    "            AS cloud_spent,"
    "        COALESCE(SUM(CASE WHEN cloud_used = 1 THEN 1 ELSE 0 END), 0)"
    "            AS cloud_ticket_count,"
    "        COALESCE(SUM(CASE WHEN local_used = 1"
    "                THEN local_input_tokens * 3.0 / 1e6"
    "                     + local_output_tokens * 15.0 / 1e6"
    "                ELSE 0 END), 0)"
    "            AS local_saved,"
    "        COALESCE(SUM(CASE WHEN local_used = 1 THEN 1 ELSE 0 END), 0)"
    "            AS local_ticket_count"
    "    FROM ticket_metrics"
    "    WHERE billing_bucket IS NOT NULL"
    "      AND (billing_bucket = 'local' OR billing_bucket = 'subscription'"
    "           OR billing_bucket = 'agent_sdk_credit')"
    "    AND recorded_at >= date('now', 'start of day')"
    "    GROUP BY billing_bucket"
)
_COST_ROLLUP_SQL_WEEK_GROUPED = (
    "SELECT billing_bucket,"
    "        COALESCE(SUM(CASE WHEN cloud_used = 1"
    "                THEN cloud_cost_estimate ELSE 0 END), 0)"
    "            AS cloud_spent,"
    "        COALESCE(SUM(CASE WHEN cloud_used = 1 THEN 1 ELSE 0 END), 0)"
    "            AS cloud_ticket_count,"
    "        COALESCE(SUM(CASE WHEN local_used = 1"
    "                THEN local_input_tokens * 3.0 / 1e6"
    "                     + local_output_tokens * 15.0 / 1e6"
    "                ELSE 0 END), 0)"
    "            AS local_saved,"
    "        COALESCE(SUM(CASE WHEN local_used = 1 THEN 1 ELSE 0 END), 0)"
    "            AS local_ticket_count"
    "    FROM ticket_metrics"
    "    WHERE billing_bucket IS NOT NULL"
    "      AND (billing_bucket = 'local' OR billing_bucket = 'subscription'"
    "           OR billing_bucket = 'agent_sdk_credit')"
    "    AND recorded_at >= date('now', '-7 days')"
    "    GROUP BY billing_bucket"
)


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

    # WOR-Sonar: columns added by ALTER TABLE ADD COLUMN over time.
    # Data-driven migration; new columns just append to this list.

    # ticket_run_log — added by ALTER TABLE ADD COLUMN over time.
    _TICKET_RUN_LOG_ADDED_COLUMNS: list[tuple[str, str]] = [
        (
            "same_epic_pair",
            (
                "ALTER TABLE ticket_run_log ADD COLUMN same_epic_pair "
                "INTEGER NOT NULL DEFAULT 0"
            ),
        ),
    ]

    # ticket_metrics — columns added by ALTER TABLE ADD COLUMN over time.
    _TICKET_METRICS_ADDED_COLUMNS: list[tuple[str, str]] = [
        # (column_name, full_alter_sql) — literal SQL keeps semgrep's
        # SQL-injection rules happy (the f-string version trips
        # python.lang.security.audit.formatted-sql-query even though
        # every value is a hardcoded class attribute).
        (
            "local_input_tokens",
            "ALTER TABLE ticket_metrics ADD COLUMN local_input_tokens INTEGER",
        ),
        (
            "local_output_tokens",
            "ALTER TABLE ticket_metrics ADD COLUMN local_output_tokens INTEGER",
        ),
        (
            "output_tokens_per_wall_second",
            "ALTER TABLE ticket_metrics ADD COLUMN output_tokens_per_wall_second REAL",
        ),
        ("change_type", "ALTER TABLE ticket_metrics ADD COLUMN change_type TEXT"),
        (
            "reasoning_demand",
            "ALTER TABLE ticket_metrics ADD COLUMN reasoning_demand INTEGER",
        ),
        (
            "scope_clarity",
            "ALTER TABLE ticket_metrics ADD COLUMN scope_clarity INTEGER",
        ),
        (
            "constraint_density",
            "ALTER TABLE ticket_metrics ADD COLUMN constraint_density INTEGER",
        ),
        (
            "ac_specificity",
            "ALTER TABLE ticket_metrics ADD COLUMN ac_specificity INTEGER",
        ),
        ("tech_stack", "ALTER TABLE ticket_metrics ADD COLUMN tech_stack TEXT"),
        ("raw_extensions", "ALTER TABLE ticket_metrics ADD COLUMN raw_extensions TEXT"),
        ("waste_score", "ALTER TABLE ticket_metrics ADD COLUMN waste_score INTEGER"),
        (
            "waste_breakdown_json",
            "ALTER TABLE ticket_metrics ADD COLUMN waste_breakdown_json TEXT",
        ),
        ("tags", "ALTER TABLE ticket_metrics ADD COLUMN tags TEXT"),
        ("notes", "ALTER TABLE ticket_metrics ADD COLUMN notes TEXT"),
        ("effort", "ALTER TABLE ticket_metrics ADD COLUMN effort TEXT"),
        (
            "compact_duration_ms",
            "ALTER TABLE ticket_metrics ADD COLUMN compact_duration_ms INTEGER",
        ),
        (
            "api_retry_count",
            "ALTER TABLE ticket_metrics ADD COLUMN api_retry_count INTEGER",
        ),
        (
            "subagent_spawns",
            "ALTER TABLE ticket_metrics ADD COLUMN subagent_spawns INTEGER",
        ),
        (
            "hook_trust_violations",
            "ALTER TABLE ticket_metrics ADD COLUMN hook_trust_violations INTEGER",
        ),
        (
            "dispatch_concurrency",
            "ALTER TABLE ticket_metrics ADD COLUMN dispatch_concurrency INTEGER",
        ),
        (
            "vllm_metrics_attributable",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_metrics_attributable INTEGER",
        ),
        (
            "vllm_prefix_cache_hits",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_prefix_cache_hits INTEGER",
        ),
        (
            "vllm_prefix_cache_queries",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_prefix_cache_queries INTEGER",
        ),
        (
            "vllm_prefix_cache_hit_ratio",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_prefix_cache_hit_ratio REAL",
        ),
        (
            "vllm_prompt_tokens",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_prompt_tokens INTEGER",
        ),
        (
            "vllm_generation_tokens",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_generation_tokens INTEGER",
        ),
        (
            "vllm_ttft_seconds_sum",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_ttft_seconds_sum REAL",
        ),
        (
            "vllm_ttft_count",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_ttft_count INTEGER",
        ),
        (
            "vllm_ttft_mean_seconds",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_ttft_mean_seconds REAL",
        ),
        (
            "vllm_preemptions",
            "ALTER TABLE ticket_metrics ADD COLUMN vllm_preemptions INTEGER",
        ),
        ("turn_count", "ALTER TABLE ticket_metrics ADD COLUMN turn_count INTEGER"),
        (
            "tool_calls_total",
            "ALTER TABLE ticket_metrics ADD COLUMN tool_calls_total INTEGER",
        ),
        (
            "tool_calls_breakdown",
            "ALTER TABLE ticket_metrics ADD COLUMN tool_calls_breakdown TEXT",
        ),
        (
            "thinking_blocks",
            "ALTER TABLE ticket_metrics ADD COLUMN thinking_blocks INTEGER",
        ),
        (
            "thinking_chars_total",
            "ALTER TABLE ticket_metrics ADD COLUMN thinking_chars_total INTEGER",
        ),
        (
            "input_tokens_max",
            "ALTER TABLE ticket_metrics ADD COLUMN input_tokens_max INTEGER",
        ),
        (
            "input_tokens_first",
            "ALTER TABLE ticket_metrics ADD COLUMN input_tokens_first INTEGER",
        ),
        (
            "input_tokens_last",
            "ALTER TABLE ticket_metrics ADD COLUMN input_tokens_last INTEGER",
        ),
        (
            "redundant_reads_count",
            "ALTER TABLE ticket_metrics ADD COLUMN redundant_reads_count INTEGER",
        ),
        (
            "billing_bucket",
            "ALTER TABLE ticket_metrics ADD COLUMN billing_bucket TEXT",
        ),
    ]

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add missing columns to ticket_metrics via PRAGMA table_info."""
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(ticket_metrics)").fetchall()
        }

        # WOR-361: legacy → new name rename. Idempotent across DB ages.
        if (
            "output_tokens_per_wall_second" not in existing
            and "local_output_tokens_per_second" in existing
        ):
            conn.execute(
                "ALTER TABLE ticket_metrics RENAME COLUMN "
                "local_output_tokens_per_second TO output_tokens_per_wall_second"
            )
            existing = {
                row[1]
                for row in conn.execute("PRAGMA table_info(ticket_metrics)").fetchall()
            }

        for col, alter_sql in self._TICKET_METRICS_ADDED_COLUMNS:
            if col not in existing:
                try:
                    conn.execute(alter_sql)
                except sqlite3.OperationalError:
                    # Concurrent migration from another process already added
                    # the column between our PRAGMA read and ALTER write
                    # (xdist test parallelism exposes this race against the
                    # shared default DB path). The column we wanted is now
                    # there, so the migration goal is satisfied either way.
                    pass

        # ticket_run_log columns
        run_log_existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(ticket_run_log)").fetchall()
        }
        for col, alter_sql in self._TICKET_RUN_LOG_ADDED_COLUMNS:
            if col not in run_log_existing:
                try:
                    conn.execute(alter_sql)
                except sqlite3.OperationalError:
                    pass  # Same race window as above; column is already there.

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
                    hook_trust_violations, dispatch_concurrency,
                    -- WOR-380: per-worker behavior telemetry
                    turn_count, tool_calls_total, tool_calls_breakdown,
                    thinking_blocks, thinking_chars_total,
                    input_tokens_max, input_tokens_first, input_tokens_last,
                    redundant_reads_count,
                    billing_bucket
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
                    :hook_trust_violations, :dispatch_concurrency,
                    :turn_count, :tool_calls_total, :tool_calls_breakdown,
                    :thinking_blocks, :thinking_chars_total,
                    :input_tokens_max, :input_tokens_first, :input_tokens_last,
                    :redundant_reads_count,
                    :billing_bucket
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

    def update_sonar_count(
        self, ticket_id: str, project_id: str, count: int | None
    ) -> None:
        """Update only the sonar_findings_count column for an existing row.

        Uses UPDATE instead of INSERT OR REPLACE so it does not rewrite the
        whole row — the initial record is still fresh, and this is a
        targeted backfill.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE ticket_metrics SET sonar_findings_count = ? "
                "WHERE ticket_id = ? AND project_id = ? "
                "AND sonar_findings_count IS NULL",
                (count, ticket_id, project_id),
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

    @overload
    def get_cost_rollup(
        self,
        period: Literal["today", "week", "all"],
        *,
        by_bucket: Literal[False] = False,
    ) -> CostRollup: ...

    @overload
    def get_cost_rollup(
        self,
        period: Literal["today", "week", "all"],
        *,
        by_bucket: Literal[True],
    ) -> dict[str, CostRollup]: ...

    def get_cost_rollup(
        self,
        period: Literal["today", "week", "all"],
        *,
        by_bucket: bool = False,
    ) -> CostRollup | dict[str, CostRollup]:
        """Return aggregated cost economics for *period*.

        *today*   = rows where ``recorded_at >= date('now', 'start of day')``
        *week*    = rows where ``recorded_at >= date('now', '-7 days')``
        *all*     = no filter

        cloud_spent  = SUM(cloud_cost_estimate) where cloud_used=1
        local_saved  = SUM(local_input_tokens * input_rate + local_output_tokens
                         * output_rate) where local_used=1; sonnet-4-6 pricing

        When *by_bucket* is True, returns a dict mapping bucket name to
        ``CostRollup`` (only ``local``, ``subscription``, ``agent_sdk_credit``
        rows are included; legacy NULL rows are excluded).
        """
        queries = {
            "today": _COST_ROLLUP_SQL_TODAY,
            "week": _COST_ROLLUP_SQL_WEEK,
            "all": _COST_ROLLUP_SQL_ALL,
        }
        grouped_queries = {
            "today": _COST_ROLLUP_SQL_TODAY_GROUPED,
            "week": _COST_ROLLUP_SQL_WEEK_GROUPED,
            "all": _COST_ROLLUP_SQL_ALL_GROUPED,
        }
        with self._connect() as conn:
            if by_bucket:
                rows = conn.execute(grouped_queries[period]).fetchall()
                return {
                    row["billing_bucket"]: CostRollup(
                        cloud_spent=row["cloud_spent"],
                        local_saved=row["local_saved"],
                        cloud_ticket_count=row["cloud_ticket_count"],
                        local_ticket_count=row["local_ticket_count"],
                    )
                    for row in rows
                }
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
                     output_tok_per_s, context_compactions, same_epic_pair)
                VALUES
                    (:ticket_id, :attempt, :implementation_mode, :outcome,
                     :failed_check, :wall_time_s, :input_tokens, :output_tokens,
                     :output_tok_per_s, :context_compactions, :same_epic_pair)
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


def _row_to_metrics(row: sqlite3.Row) -> TicketMetrics:
    d = dict(row)
    d["cloud_used"] = bool(d["cloud_used"])
    d["local_used"] = bool(d["local_used"])
    d["escalated_to_cloud"] = bool(d["escalated_to_cloud"])
    raw_failures = d.pop("check_failures_json", None)
    d["check_failures"] = json.loads(raw_failures) if raw_failures is not None else None
    d.pop("recorded_at", None)
    return TicketMetrics.model_validate(d)
