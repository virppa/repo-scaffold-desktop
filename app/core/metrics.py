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
from pathlib import Path
from typing import Generator, Literal

from pydantic import BaseModel, Field

ImplementationMode = Literal["local", "cloud", "hybrid"]
Outcome = Literal["success", "failure", "escalated", "aborted"]
CheckOutcome = Literal["passed", "failed"]

_APP_DIR = "repo-scaffold"
_DB_NAME = "metrics.db"

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

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS ticket_metrics (
    ticket_id                      TEXT NOT NULL,
    project_id                     TEXT NOT NULL,
    epic_id                        TEXT,
    implementation_mode            TEXT NOT NULL,
    cloud_used                     INTEGER NOT NULL DEFAULT 0,
    cloud_model                    TEXT,
    cloud_tokens                   INTEGER,
    cloud_cost_estimate            REAL,
    local_used                     INTEGER NOT NULL DEFAULT 0,
    local_model                    TEXT,
    local_input_tokens             INTEGER,
    local_output_tokens            INTEGER,
    local_tokens                   INTEGER,
    local_wall_time                REAL,
    local_output_tokens_per_second REAL,
    escalated_to_cloud             INTEGER NOT NULL DEFAULT 0,
    outcome                        TEXT NOT NULL,
    retry_count                    INTEGER NOT NULL DEFAULT 0,
    check_failures_json            TEXT,
    lines_changed                  INTEGER,
    files_changed                  INTEGER,
    sonar_findings_count           INTEGER,
    context_compactions            INTEGER,
    change_type                    TEXT,
    reasoning_demand               TEXT,
    scope_clarity                  TEXT,
    constraint_density             TEXT,
    ac_specificity                 TEXT,
    multi_file_consistency_required INTEGER NOT NULL DEFAULT 0,
    is_greenfield                  INTEGER NOT NULL DEFAULT 0,
    has_external_dependency        INTEGER NOT NULL DEFAULT 0,
    tech_stack                     TEXT,
    raw_extensions                 TEXT,
    recorded_at                    TEXT NOT NULL DEFAULT (datetime('now')),
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
    local_output_tokens_per_second: float | None = Field(
        default=None, description="output_tokens / wall_time when both present"
    )
    local_wall_time: float | None = Field(default=None, description="Seconds")
    escalated_to_cloud: bool = False
    outcome: Outcome
    retry_count: int = 0
    check_failures: dict[str, int] | None = Field(
        default=None,
        description="Per-check failure counts, e.g. {'mypy': 2, 'pytest': 1}",
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

    # TaskProfile fields (WOR-216)
    change_type: str | None = Field(
        default=None,
        description="change_type from TaskProfile: "
        "(bugfix/feature/refactor/api_integration/architectural/test/docs)",
    )
    reasoning_demand: str | None = Field(
        default=None,
        description="reasoning_demand from TaskProfile (mechanical/analytical/design)",
    )
    scope_clarity: str | None = Field(
        default=None,
        description="scope_clarity from TaskProfile (specified/inferred/ambiguous)",
    )
    constraint_density: str | None = Field(
        default=None,
        description="constraint_density from TaskProfile (low/medium/high)",
    )
    ac_specificity: str | None = Field(
        default=None,
        description="ac_specificity from TaskProfile (testable/behavioral/vague)",
    )
    multi_file_consistency_required: bool = False
    is_greenfield: bool = False
    has_external_dependency: bool = False
    tech_stack: list[str] | None = Field(
        default=None,
        description="List of tech stack literals from TaskProfile",
    )
    raw_extensions: list[str] | None = Field(
        default=None,
        description="List of raw file extensions from TaskProfile",
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
        if "local_output_tokens_per_second" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics "
                "ADD COLUMN local_output_tokens_per_second REAL"
            )
        # TaskProfile columns (WOR-216)
        if "change_type" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN change_type TEXT")
        if "reasoning_demand" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN reasoning_demand TEXT")
        if "scope_clarity" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN scope_clarity TEXT")
        if "constraint_density" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics ADD COLUMN constraint_density TEXT"
            )
        if "ac_specificity" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN ac_specificity TEXT")
        if "multi_file_consistency_required" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics "
                "ADD COLUMN multi_file_consistency_required INTEGER NOT NULL DEFAULT 0"
            )
        if "is_greenfield" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics "
                "ADD COLUMN is_greenfield INTEGER NOT NULL DEFAULT 0"
            )
        if "has_external_dependency" not in existing:
            conn.execute(
                "ALTER TABLE ticket_metrics "
                "ADD COLUMN has_external_dependency INTEGER NOT NULL DEFAULT 0"
            )
        if "tech_stack" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN tech_stack TEXT")
        if "raw_extensions" not in existing:
            conn.execute("ALTER TABLE ticket_metrics ADD COLUMN raw_extensions TEXT")

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
                    local_tokens, local_wall_time, local_output_tokens_per_second,
                    escalated_to_cloud, outcome,
                    retry_count, check_failures_json,
                    lines_changed, files_changed,
                    sonar_findings_count, context_compactions,
                    change_type, reasoning_demand, scope_clarity,
                    constraint_density, ac_specificity,
                    multi_file_consistency_required, is_greenfield,
                    has_external_dependency, tech_stack, raw_extensions
                ) VALUES (
                    :ticket_id, :project_id, :epic_id, :implementation_mode,
                    :cloud_used, :cloud_model, :cloud_tokens, :cloud_cost_estimate,
                    :local_used, :local_model,
                    :local_input_tokens, :local_output_tokens,
                    :local_tokens, :local_wall_time,
                    :local_output_tokens_per_second,
                    :escalated_to_cloud, :outcome,
                    :retry_count, :check_failures_json,
                    :lines_changed, :files_changed,
                    :sonar_findings_count, :context_compactions,
                    :change_type, :reasoning_demand, :scope_clarity,
                    :constraint_density, :ac_specificity,
                    :multi_file_consistency_required, :is_greenfield,
                    :has_external_dependency, :tech_stack, :raw_extensions
                )
                """,
                {
                    **metrics.model_dump(
                        exclude={"check_failures", "tech_stack", "raw_extensions"},
                    ),
                    "cloud_used": int(metrics.cloud_used),
                    "local_used": int(metrics.local_used),
                    "escalated_to_cloud": int(metrics.escalated_to_cloud),
                    "check_failures_json": (
                        json.dumps(metrics.check_failures)
                        if metrics.check_failures is not None
                        else None
                    ),
                    "multi_file_consistency_required": int(
                        metrics.multi_file_consistency_required,
                    ),
                    "is_greenfield": int(metrics.is_greenfield),
                    "has_external_dependency": int(metrics.has_external_dependency),
                    "tech_stack": (
                        metrics.tech_stack
                        if isinstance(metrics.tech_stack, str)
                        else json.dumps(metrics.tech_stack)
                        if metrics.tech_stack
                        else None
                    ),
                    "raw_extensions": (
                        metrics.raw_extensions
                        if isinstance(metrics.raw_extensions, str)
                        else json.dumps(metrics.raw_extensions)
                        if metrics.raw_extensions
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
    d["multi_file_consistency_required"] = bool(
        d.get("multi_file_consistency_required"),
    )
    d["is_greenfield"] = bool(d.get("is_greenfield"))
    d["has_external_dependency"] = bool(d.get("has_external_dependency"))
    raw_failures = d.pop("check_failures_json", None)
    d["check_failures"] = json.loads(raw_failures) if raw_failures is not None else None
    # Parse JSON-encoded tech_stack and raw_extensions
    raw_tech_stack = d.pop("tech_stack", None)
    d["tech_stack"] = json.loads(raw_tech_stack) if raw_tech_stack is not None else None
    raw_raw_ext = d.pop("raw_extensions", None)
    d["raw_extensions"] = json.loads(raw_raw_ext) if raw_raw_ext is not None else None
    d.pop("recorded_at", None)
    return TicketMetrics.model_validate(d)
