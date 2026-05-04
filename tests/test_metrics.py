"""Tests for app.core.metrics — MetricsStore and TicketMetrics."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.metrics import (
    CheckRunEntry,
    CheckStats,
    EpicSummary,
    MetricsStore,
    TicketMetrics,
    TicketRunLog,
    compute_tags,
)


def _store(tmp_path) -> MetricsStore:
    return MetricsStore(db_path=tmp_path / "app.db")


def _ticket(**kwargs) -> TicketMetrics:
    defaults: dict = {
        "ticket_id": "WOR-1",
        "project_id": "proj-a",
        "epic_id": "WOR-10",
        "implementation_mode": "local",
        "local_used": True,
        "local_model": "qwen3-coder",
        "local_tokens": 8000,
        "local_wall_time": 120.5,
        "outcome": "success",
    }
    defaults.update(kwargs)
    return TicketMetrics(**defaults)


class TestSchemaCreation:
    def test_db_file_created_on_init(self, tmp_path):
        db = tmp_path / "app.db"
        assert not db.exists()
        MetricsStore(db_path=db)
        assert db.exists()

    def test_second_init_does_not_raise(self, tmp_path):
        MetricsStore(db_path=tmp_path / "app.db")
        MetricsStore(db_path=tmp_path / "app.db")


class TestRecordAndRetrieve:
    def test_insert_and_retrieve_by_ticket(self, tmp_path):
        store = _store(tmp_path)
        m = _ticket()
        store.record(m)
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.ticket_id == "WOR-1"
        assert result.local_tokens == 8000
        assert result.outcome == "success"

    def test_missing_ticket_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.get_by_ticket("WOR-99", "proj-a") is None

    def test_upsert_last_write_wins(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(local_tokens=100))
        store.record(_ticket(local_tokens=999))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.local_tokens == 999

    def test_bool_fields_round_trip(self, tmp_path):
        store = _store(tmp_path)
        m = _ticket(cloud_used=True, escalated_to_cloud=True, local_used=True)
        store.record(m)
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.cloud_used is True
        assert result.escalated_to_cloud is True

    def test_nullable_fields_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(cloud_model=None, cloud_tokens=None))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.cloud_model is None
        assert result.cloud_tokens is None


class TestCheckFailures:
    def test_check_failures_round_trip(self, tmp_path):
        store = _store(tmp_path)
        failures = [
            {"check": "mypy", "exit_code": 1},
            {"check": "pytest", "exit_code": 2},
        ]
        store.record(_ticket(check_failures=failures))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.check_failures == failures

    def test_none_check_failures_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(check_failures=None))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.check_failures is None

    def test_empty_check_failures_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(check_failures=[]))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.check_failures == []


class TestTaxonomyColumns:
    """WOR-262: 7 ticket taxonomy columns (change_type, reasoning_demand, etc.)."""

    def test_taxonomy_round_trip(self, tmp_path):
        store = _store(tmp_path)
        m = _ticket(
            change_type="additive",
            reasoning_demand=4,
            scope_clarity=5,
            constraint_density=2,
            ac_specificity=4,
            tech_stack="python,sqlite,pydantic",
            raw_extensions='[".py",".md"]',
        )
        store.record(m)
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.change_type == "additive"
        assert result.reasoning_demand == 4
        assert result.scope_clarity == 5
        assert result.constraint_density == 2
        assert result.ac_specificity == 4
        assert result.tech_stack == "python,sqlite,pydantic"
        assert result.raw_extensions == '[".py",".md"]'

    def test_taxonomy_defaults_to_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.change_type is None
        assert result.reasoning_demand is None
        assert result.scope_clarity is None
        assert result.constraint_density is None
        assert result.ac_specificity is None
        assert result.tech_stack is None
        assert result.raw_extensions is None


class TestEffortColumn:
    """WOR-348: persist effort (low/medium/high/xhigh/max) for retro analytics."""

    def test_effort_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(effort="xhigh"))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.effort == "xhigh"

    def test_effort_defaults_to_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.effort is None

    def test_effort_accepts_all_levels(self, tmp_path):
        store = _store(tmp_path)
        for level in ("low", "medium", "high", "xhigh", "max"):
            store.record(_ticket(ticket_id=f"WOR-{level}", effort=level))
            result = store.get_by_ticket(f"WOR-{level}", "proj-a")
            assert result is not None
            assert result.effort == level

    def test_effort_migration_idempotent(self, tmp_path):
        """Calling _migrate twice does not raise on the effort column."""
        store = _store(tmp_path)
        # First migration runs at __init__; call again to ensure idempotency.
        with store._connect() as conn:
            store._migrate(conn)
            store._migrate(conn)


class TestCompactDurationColumn:
    """WOR-358: persist total compaction time per session."""

    def test_compact_duration_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(compact_duration_ms=88463))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.compact_duration_ms == 88463

    def test_compact_duration_defaults_to_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.compact_duration_ms is None

    def test_compact_duration_zero_distinct_from_none(self, tmp_path):
        """Zero (no compactions) is a valid value, distinct from None (unknown)."""
        store = _store(tmp_path)
        store.record(_ticket(compact_duration_ms=0))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.compact_duration_ms == 0
        assert result.compact_duration_ms is not None

    def test_compact_duration_migration_idempotent(self, tmp_path):
        store = _store(tmp_path)
        with store._connect() as conn:
            store._migrate(conn)
            store._migrate(conn)


class TestApiRetryColumn:
    """WOR-360: persist Claude Code's internal api_retry count per session."""

    def test_api_retry_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(api_retry_count=6))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.api_retry_count == 6

    def test_api_retry_defaults_to_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.api_retry_count is None

    def test_api_retry_zero_distinct_from_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(api_retry_count=0))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.api_retry_count == 0
        assert result.api_retry_count is not None

    def test_api_retry_migration_idempotent(self, tmp_path):
        store = _store(tmp_path)
        with store._connect() as conn:
            store._migrate(conn)
            store._migrate(conn)


class TestSubagentSpawnsColumn:
    """WOR-364: persist Task-tool subagent count per session."""

    def test_subagent_spawns_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(subagent_spawns=4))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.subagent_spawns == 4

    def test_subagent_spawns_defaults_to_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.subagent_spawns is None

    def test_subagent_spawns_zero_distinct_from_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(subagent_spawns=0))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.subagent_spawns == 0
        assert result.subagent_spawns is not None

    def test_subagent_spawns_migration_idempotent(self, tmp_path):
        store = _store(tmp_path)
        with store._connect() as conn:
            store._migrate(conn)
            store._migrate(conn)


class TestDispatchConcurrencyColumn:
    """WOR-363: persist count of OTHER active workers at dispatch time."""

    def test_dispatch_concurrency_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(dispatch_concurrency=3))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.dispatch_concurrency == 3

    def test_dispatch_concurrency_solo_is_zero(self, tmp_path):
        """Zero (solo dispatch) is a valid value, distinct from None."""
        store = _store(tmp_path)
        store.record(_ticket(dispatch_concurrency=0))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.dispatch_concurrency == 0
        assert result.dispatch_concurrency is not None

    def test_dispatch_concurrency_defaults_to_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.dispatch_concurrency is None

    def test_dispatch_concurrency_migration_idempotent(self, tmp_path):
        store = _store(tmp_path)
        with store._connect() as conn:
            store._migrate(conn)
            store._migrate(conn)


class TestAdditionalMetrics:
    def test_retry_and_diff_metrics_round_trip(self, tmp_path):
        store = _store(tmp_path)
        m = _ticket(
            retry_count=3,
            lines_changed=42,
            files_changed=5,
            sonar_findings_count=2,
            context_compactions=1,
        )
        store.record(m)
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.retry_count == 3
        assert result.lines_changed == 42
        assert result.files_changed == 5
        assert result.sonar_findings_count == 2
        assert result.context_compactions == 1


class TestGetByEpic:
    def test_retrieve_all_tickets_for_epic(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(ticket_id="WOR-1", epic_id="WOR-10"))
        store.record(_ticket(ticket_id="WOR-2", epic_id="WOR-10"))
        store.record(_ticket(ticket_id="WOR-3", epic_id="WOR-20"))
        results = store.get_by_epic("WOR-10", "proj-a")
        assert len(results) == 2
        assert {r.ticket_id for r in results} == {"WOR-1", "WOR-2"}

    def test_empty_epic_returns_empty_list(self, tmp_path):
        store = _store(tmp_path)
        assert store.get_by_epic("WOR-99", "proj-a") == []


class TestEpicSummary:
    def test_rollup_sums_all_fields(self, tmp_path):
        store = _store(tmp_path)
        store.record(
            _ticket(
                ticket_id="WOR-1",
                epic_id="WOR-10",
                cloud_tokens=1000,
                cloud_cost_estimate=0.10,
                local_tokens=500,
                local_wall_time=60.0,
                escalated_to_cloud=True,
                retry_count=2,
                lines_changed=10,
                files_changed=2,
                sonar_findings_count=1,
            )
        )
        store.record(
            _ticket(
                ticket_id="WOR-2",
                epic_id="WOR-10",
                cloud_tokens=2000,
                cloud_cost_estimate=0.20,
                local_tokens=300,
                local_wall_time=30.0,
                escalated_to_cloud=False,
                retry_count=1,
                lines_changed=5,
                files_changed=1,
                sonar_findings_count=0,
            )
        )
        summary = store.epic_summary("WOR-10", "proj-a")
        assert isinstance(summary, EpicSummary)
        assert summary.ticket_count == 2
        assert summary.cloud_tokens_total == 3000
        assert summary.cloud_cost_total == pytest.approx(0.30)
        assert summary.local_tokens_total == 800
        assert summary.local_wall_time_total == pytest.approx(90.0)
        assert summary.escalation_count == 1
        assert summary.retry_count_total == 3
        assert summary.lines_changed_total == 15
        assert summary.files_changed_total == 3
        assert summary.sonar_findings_total == 1

    def test_empty_epic_summary_returns_zeros(self, tmp_path):
        store = _store(tmp_path)
        summary = store.epic_summary("WOR-99", "proj-a")
        assert summary.ticket_count == 0
        assert summary.cloud_tokens_total == 0
        assert summary.cloud_cost_total == 0.0
        assert summary.escalation_count == 0
        assert summary.retry_count_total == 0


class TestProjectIsolation:
    def test_different_projects_do_not_share_records(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket(ticket_id="WOR-1", project_id="proj-a"))
        store.record(_ticket(ticket_id="WOR-1", project_id="proj-b", local_tokens=9999))
        a = store.get_by_ticket("WOR-1", "proj-a")
        b = store.get_by_ticket("WOR-1", "proj-b")
        assert a is not None and b is not None
        assert a.local_tokens == 8000
        assert b.local_tokens == 9999

    def test_epic_summary_scoped_to_project(self, tmp_path):
        store = _store(tmp_path)
        store.record(
            _ticket(
                ticket_id="WOR-1",
                project_id="proj-a",
                epic_id="WOR-10",
                cloud_tokens=100,
            )
        )
        store.record(
            _ticket(
                ticket_id="WOR-1",
                project_id="proj-b",
                epic_id="WOR-10",
                cloud_tokens=999,
            )
        )
        summary = store.epic_summary("WOR-10", "proj-a")
        assert summary.cloud_tokens_total == 100


def _check_run(ticket_id: str = "WOR-1", **kwargs) -> CheckRunEntry:
    defaults: dict = {
        "ticket_id": ticket_id,
        "project_id": "proj-a",
        "check_cmd": "pytest",
        "outcome": "passed",
        "duration_s": 5.0,
    }
    defaults.update(kwargs)
    return CheckRunEntry(**defaults)


class TestCheckRunLog:
    def test_record_and_check_stats_basic(self, tmp_path):
        store = _store(tmp_path)
        store.record_check_run(
            _check_run(check_cmd="pytest", outcome="passed", duration_s=10.0)
        )
        store.record_check_run(
            _check_run(check_cmd="pytest", outcome="failed", duration_s=8.0)
        )
        stats = store.get_check_stats("proj-a")
        assert len(stats) == 1
        s = stats[0]
        assert isinstance(s, CheckStats)
        assert s.check_cmd == "pytest"
        assert s.total_runs == 2
        assert s.pass_count == 1
        assert s.fail_count == 1
        assert s.pass_pct == pytest.approx(50.0)
        assert s.avg_duration_s == pytest.approx(9.0)
        assert s.max_duration_s == pytest.approx(10.0)

    def test_multiple_checks_ordered_slowest_first(self, tmp_path):
        store = _store(tmp_path)
        store.record_check_run(
            _check_run(check_cmd="mypy app/", outcome="passed", duration_s=20.0)
        )
        store.record_check_run(
            _check_run(check_cmd="ruff check .", outcome="passed", duration_s=2.0)
        )
        store.record_check_run(
            _check_run(check_cmd="pytest", outcome="passed", duration_s=10.0)
        )
        stats = store.get_check_stats("proj-a")
        assert [s.check_cmd for s in stats] == ["mypy app/", "pytest", "ruff check ."]

    def test_null_duration_handled(self, tmp_path):
        store = _store(tmp_path)
        store.record_check_run(_check_run(duration_s=None))
        stats = store.get_check_stats("proj-a")
        assert stats[0].avg_duration_s is None
        assert stats[0].max_duration_s is None

    def test_empty_project_returns_empty_list(self, tmp_path):
        store = _store(tmp_path)
        assert store.get_check_stats("proj-z") == []

    def test_project_isolation(self, tmp_path):
        store = _store(tmp_path)
        store.record_check_run(_check_run(project_id="proj-a", duration_s=5.0))
        store.record_check_run(_check_run(project_id="proj-b", duration_s=99.0))
        a_stats = store.get_check_stats("proj-a")
        b_stats = store.get_check_stats("proj-b")
        assert len(a_stats) == 1 and len(b_stats) == 1
        assert a_stats[0].avg_duration_s == pytest.approx(5.0)
        assert b_stats[0].avg_duration_s == pytest.approx(99.0)

    def test_check_run_log_does_not_affect_ticket_metrics(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        store.record_check_run(_check_run())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.ticket_id == "WOR-1"


class TestMigration:
    def test_migration_adds_new_columns(self, tmp_path):
        """Existing DB gets local_input_tokens, local_output_tokens,
        output_tokens_per_wall_second without error."""
        store = MetricsStore(db_path=tmp_path / "app.db")
        # Write and read before _migrate runs to establish baseline
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        # Re-create store (simulating re-open after DB init)
        store2 = MetricsStore(db_path=tmp_path / "app.db")
        store2.record(_ticket())
        result2 = store2.get_by_ticket("WOR-1", "proj-a")
        assert result2 is not None

    def test_new_columns_stored_and_retrieved(self, tmp_path):
        """New token fields round-trip through the DB."""
        store = _store(tmp_path)
        store.record(
            _ticket(
                local_input_tokens=10000,
                local_output_tokens=500,
                output_tokens_per_wall_second=4.17,
            )
        )
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.local_input_tokens == 10000
        assert result.local_output_tokens == 500
        assert result.output_tokens_per_wall_second == pytest.approx(4.17)

    def test_new_columns_none_default(self, tmp_path):
        """Fields default to None when not provided."""
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result.local_input_tokens is None
        assert result.local_output_tokens is None
        assert result.output_tokens_per_wall_second is None

    def test_backward_compat_local_tokens_preserved(self, tmp_path):
        """local_tokens remains valid alongside new fields."""
        store = _store(tmp_path)
        store.record(
            _ticket(
                local_input_tokens=10000,
                local_output_tokens=500,
                local_tokens=10500,
            )
        )
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result.local_tokens == 10500
        assert result.local_input_tokens == 10000
        assert result.local_output_tokens == 500


class TestWasteColumns:
    """WOR-277: waste_score and waste_breakdown_json columns."""

    def test_waste_score_round_trip(self, tmp_path):
        store = _store(tmp_path)
        store.record(
            _ticket(
                waste_score=72,
                waste_breakdown_json='{"redundant_reads": 5, "manual_check_runs": 3}',
            )
        )
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.waste_score == 72
        expected = '{"redundant_reads": 5, "manual_check_runs": 3}'
        assert result.waste_breakdown_json == expected

    def test_waste_score_defaults_to_none(self, tmp_path):
        store = _store(tmp_path)
        store.record(_ticket())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.waste_score is None
        assert result.waste_breakdown_json is None

    def test_waste_score_zero_stored(self, tmp_path):
        """A score of 0 is stored (not treated as None)."""
        store = _store(tmp_path)
        store.record(_ticket(waste_score=0, waste_breakdown_json="{}"))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.waste_score == 0
        assert result.waste_breakdown_json == "{}"


def _run_log(**kwargs) -> TicketRunLog:
    defaults: dict = {
        "ticket_id": "WOR-1",
        "attempt": 1,
        "implementation_mode": "local",
        "outcome": "success",
        "wall_time_s": 120.5,
        "input_tokens": 10000,
        "output_tokens": 500,
        "output_tok_per_s": 4.17,
    }
    defaults.update(kwargs)
    return TicketRunLog(**defaults)


class TestTicketRunLog:
    def test_multi_attempt_produces_two_rows(self, tmp_path):
        """Two finalize calls for the same ticket produce two run_log rows
        with attempt=1 and attempt=2 (WOR-259)."""
        store = _store(tmp_path)
        store.record_run(_run_log(attempt=1, wall_time_s=100.0))
        store.record_run(_run_log(attempt=2, wall_time_s=150.0))
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT id, attempt, wall_time_s FROM ticket_run_log ORDER BY attempt"
            ).fetchall()
        assert len(rows) == 2
        assert rows[0]["attempt"] == 1
        assert rows[0]["wall_time_s"] == 100.0
        assert rows[1]["attempt"] == 2
        assert rows[1]["wall_time_s"] == 150.0

    def test_append_only_no_overwrite(self, tmp_path):
        """Same attempt appended again — does not replace existing row."""
        store = _store(tmp_path)
        store.record_run(_run_log(attempt=1, output_tokens=500))
        store.record_run(_run_log(attempt=1, output_tokens=600))
        with store._connect() as conn:
            rows = conn.execute(
                "SELECT output_tokens FROM ticket_run_log WHERE attempt = 1"
            ).fetchall()
        assert len(rows) == 2
        assert rows[0]["output_tokens"] == 500
        assert rows[1]["output_tokens"] == 600

    def test_same_ticket_different_attempts(self, tmp_path):
        """Multiple tickets can each have multi-attempt rows."""
        store = _store(tmp_path)
        store.record_run(_run_log(ticket_id="WOR-1", attempt=1))
        store.record_run(_run_log(ticket_id="WOR-1", attempt=2))
        store.record_run(_run_log(ticket_id="WOR-2", attempt=1))
        with store._connect() as conn:
            w1 = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_run_log WHERE ticket_id = 'WOR-1'"
            ).fetchone()
            w2 = conn.execute(
                "SELECT COUNT(*) AS c FROM ticket_run_log WHERE ticket_id = 'WOR-2'"
            ).fetchone()
        assert w1["c"] == 2
        assert w2["c"] == 1

    def test_null_fields_round_trip(self, tmp_path):
        """Nullable fields stored as NULL in SQLite."""
        store = _store(tmp_path)
        store.record_run(
            _run_log(
                failed_check=None,
                wall_time_s=None,
                input_tokens=None,
                output_tokens=None,
                output_tok_per_s=None,
                context_compactions=None,
            )
        )
        with store._connect() as conn:
            row = conn.execute("SELECT * FROM ticket_run_log").fetchone()
        assert row["failed_check"] is None
        assert row["wall_time_s"] is None
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["output_tok_per_s"] is None
        assert row["context_compactions"] is None


# ---------------------------------------------------------------------------
# compute_tags — individual rule tests (9 rules)
# ---------------------------------------------------------------------------


def _metrics(**kwargs) -> TicketMetrics:
    defaults: dict = {
        "ticket_id": "WOR-1",
        "project_id": "proj-a",
        "implementation_mode": "local",
        "outcome": "success",
        "retry_count": 0,
        "lines_changed": 10,
        "waste_score": None,
        "local_tokens": None,
        "local_wall_time": None,
    }
    defaults.update(kwargs)
    return TicketMetrics(**defaults)


class TestComputeTags:
    """compute_tags — 9 rules, multi-rule, no-rule, set_tags, _migrate."""

    # --- Anomaly detection rules (4) ---

    def test_zero_tokens_high_wall_time(self):
        """zero_tokens_high_wall_time: local_tokens < 100k AND
        local_wall_time > 1800."""
        m = _metrics(
            outcome="failure",
            local_tokens=50_000,
            local_wall_time=2_000,
        )
        assert "zero_tokens_high_wall_time" in compute_tags(m, "success")

    def test_zero_tokens_high_wall_time_below_threshold(self):
        """Does not fire when local_tokens >= 100k."""
        m = _metrics(
            outcome="failure",
            local_tokens=200_000,
            local_wall_time=2_000,
        )
        assert "zero_tokens_high_wall_time" not in compute_tags(m, "success")

    def test_zero_tokens_high_wall_time_low_wall_time(self):
        """Does not fire when local_wall_time <= 1800."""
        m = _metrics(
            outcome="failure",
            local_tokens=50_000,
            local_wall_time=1_000,
        )
        assert "zero_tokens_high_wall_time" not in compute_tags(m, "success")

    def test_no_diff_against_base(self):
        """no_diff_against_base: lines_changed == 0 AND outcome == 'failure'."""
        m = _metrics(outcome="failure", lines_changed=0)
        assert "no_diff_against_base" in compute_tags(m, "success")

    def test_no_diff_against_base_success_outcome(self):
        """Does not fire when outcome != 'failure'."""
        m = _metrics(outcome="success", lines_changed=0)
        assert "no_diff_against_base" not in compute_tags(m, "success")

    def test_no_diff_against_base_nonzero_lines(self):
        """Does not fire when lines_changed > 0."""
        m = _metrics(outcome="failure", lines_changed=5)
        assert "no_diff_against_base" not in compute_tags(m, "success")

    def test_success_outcome_state_mismatch(self):
        """success_outcome_state_mismatch: result.json=success AND
        metrics outcome=failure."""
        m = _metrics(outcome="failure")
        assert "success_outcome_state_mismatch" in compute_tags(m, "success")

    def test_success_outcome_state_mismatch_no_mismatch(self):
        """Does not fire when outcome matches result_json_status."""
        m = _metrics(outcome="success")
        assert "success_outcome_state_mismatch" not in compute_tags(m, "success")

    def test_success_pr_create_failed(self):
        """success_pr_create_failed: outcome=success AND tracked_prs is empty list."""
        m = _metrics(outcome="success")
        assert "success_pr_create_failed" in compute_tags(m, "success", tracked_prs=[])

    def test_success_pr_create_failed_with_pr(self):
        """Does not fire when tracked_prs has entries."""
        m = _metrics(outcome="success")
        assert "success_pr_create_failed" not in compute_tags(
            m, "success", tracked_prs=[MagicMock()]
        )

    def test_success_pr_create_failed_none_tracked(self):
        """Does not fire when tracked_prs is None."""
        m = _metrics(outcome="success")
        assert "success_pr_create_failed" not in compute_tags(m, "success")

    # --- Categorization rules (4) ---

    def test_scope_drift(self):
        """scope_drift: result_json_flags['scope_drift'] is true."""
        m = _metrics()
        assert "scope_drift" in compute_tags(m, "success", {"scope_drift": True})

    def test_scope_drift_false(self):
        """Does not fire when scope_drift is false."""
        m = _metrics()
        assert "scope_drift" not in compute_tags(m, "success", {"scope_drift": False})

    def test_escalated(self):
        """escalated: outcome == 'escalated'."""
        m = _metrics(outcome="escalated")
        assert "escalated" in compute_tags(m, "success")

    def test_escalated_not_fired_for_other_outcomes(self):
        """Does not fire when outcome is not 'escalated'."""
        m = _metrics(outcome="success")
        assert "escalated" not in compute_tags(m, "success")

    def test_high_waste(self):
        """high_waste: waste_score > 80."""
        m = _metrics(waste_score=85)
        assert "high_waste" in compute_tags(m, "success")

    def test_high_waste_below_threshold(self):
        """Does not fire when waste_score <= 80."""
        m = _metrics(waste_score=80)
        assert "high_waste" not in compute_tags(m, "success")

    def test_high_waste_none(self):
        """Does not fire when waste_score is None."""
        m = _metrics(waste_score=None)
        assert "high_waste" not in compute_tags(m, "success")

    def test_rework(self):
        """rework: retry_count > 0."""
        m = _metrics(retry_count=2)
        assert "rework" in compute_tags(m, "success")

    def test_rework_zero(self):
        """Does not fire when retry_count == 0."""
        m = _metrics(retry_count=0)
        assert "rework" not in compute_tags(m, "success")

    # --- WOR-366: backend_unstable rule ---

    def test_backend_unstable_at_threshold(self):
        """backend_unstable: api_retry_count >= 6."""
        m = _metrics(api_retry_count=6)
        assert "backend_unstable" in compute_tags(m, "success")

    def test_backend_unstable_max_retries(self):
        """Fires for high counts (e.g. WOR-317 had 19 retries)."""
        m = _metrics(api_retry_count=19)
        assert "backend_unstable" in compute_tags(m, "success")

    def test_backend_unstable_below_threshold(self):
        """Does not fire at api_retry_count == 5 (boundary)."""
        m = _metrics(api_retry_count=5)
        assert "backend_unstable" not in compute_tags(m, "success")

    def test_backend_unstable_zero(self):
        """Does not fire when api_retry_count == 0."""
        m = _metrics(api_retry_count=0)
        assert "backend_unstable" not in compute_tags(m, "success")

    def test_backend_unstable_none(self):
        """Does not fire when api_retry_count is None (defensive)."""
        m = _metrics(api_retry_count=None)
        assert "backend_unstable" not in compute_tags(m, "success")

    def test_mid_session_compaction_when_one(self):
        """Fires when context_compactions is 1."""
        m = _metrics(context_compactions=1)
        assert "mid_session_compaction" in compute_tags(m, "success")

    def test_mid_session_compaction_when_two(self):
        """Fires when context_compactions is 2."""
        m = _metrics(context_compactions=2)
        assert "mid_session_compaction" in compute_tags(m, "success")

    def test_mid_session_compaction_not_when_zero(self):
        """Does not fire when context_compactions is 0."""
        m = _metrics(context_compactions=0)
        assert "mid_session_compaction" not in compute_tags(m, "success")

    def test_mid_session_compaction_not_when_none(self):
        """Does not fire when context_compactions is None."""
        m = _metrics(context_compactions=None)
        assert "mid_session_compaction" not in compute_tags(m, "success")

    # --- Multi-rule and no-rule ---

    def test_multi_rule_match(self):
        """Multiple rules can fire simultaneously."""
        m = _metrics(
            outcome="escalated",
            retry_count=3,
            waste_score=90,
            lines_changed=0,
        )
        tags = compute_tags(m, "success", {"scope_drift": True})
        assert "escalated" in tags
        assert "rework" in tags
        assert "high_waste" in tags
        # outcome is 'escalated', not 'failure'
        assert "no_diff_against_base" not in tags

    def test_all_rules_match(self):
        """All 8 rules fire when conditions are met."""
        m = _metrics(
            outcome="escalated",
            retry_count=5,
            waste_score=95,
            lines_changed=0,
            local_tokens=50_000,
            local_wall_time=3_000,
        )
        tags = compute_tags(
            m,
            "success",
            {"scope_drift": True},
            tracked_prs=[],
        )
        assert set(tags) == {
            "zero_tokens_high_wall_time",
            "escalated",
            "high_waste",
            "rework",
            "scope_drift",
        }
        # outcome is 'escalated' not 'failure', so
        # no_diff_against_base doesn't fire
        # outcome is 'escalated' not 'failure', so
        # success_outcome_state_mismatch doesn't fire

    def test_no_rule_match_returns_empty(self):
        """No matching conditions → empty list."""
        m = _metrics(outcome="success", retry_count=0, waste_score=None)
        assert compute_tags(m, "success") == []

    def test_no_rule_match_with_none_values(self):
        """Missing fields don't cause errors."""
        m = _metrics(outcome="success", local_tokens=None, local_wall_time=None)
        assert compute_tags(m, "success") == []

    # --- set_tags round-trip ---

    def test_set_tags_round_trip(self, tmp_path):
        """set_tags writes tags, get_by_ticket reads them back."""
        store = MetricsStore(db_path=tmp_path / "app.db")
        m = _metrics()
        store.record(m)
        store.set_tags("WOR-1", "proj-a", ["high_waste", "rework"])
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.tags == '["high_waste", "rework"]'

    def test_set_tags_overwrites(self, tmp_path):
        """Calling set_tags again overwrites previous tags."""
        store = MetricsStore(db_path=tmp_path / "app.db")
        store.record(_metrics())
        store.set_tags("WOR-1", "proj-a", ["tag1"])
        store.set_tags("WOR-1", "proj-a", ["tag2", "tag3"])
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.tags == '["tag2", "tag3"]'

    def test_set_tags_empty_clears(self, tmp_path):
        """Calling set_tags with empty list clears the tags column."""
        store = MetricsStore(db_path=tmp_path / "app.db")
        store.record(_metrics())
        store.set_tags("WOR-1", "proj-a", ["tag1"])
        store.set_tags("WOR-1", "proj-a", [])
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.tags is None

    # --- _migrate idempotency ---

    def test_migrate_idempotent(self, tmp_path):
        """Running _migrate twice does not raise and does not duplicate columns."""
        MetricsStore(db_path=tmp_path / "app.db")
        MetricsStore(db_path=tmp_path / "app.db")
        # If we got here without raising, idempotency is satisfied.

    def test_migrate_tags_column_added(self, tmp_path):
        """tags column is present after second init."""
        MetricsStore(db_path=tmp_path / "app.db")
        MetricsStore(db_path=tmp_path / "app.db")
        store = MetricsStore(db_path=tmp_path / "app.db")
        store.record(_metrics(tags='["test"]'))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.tags == '["test"]'

    def test_migrate_notes_column_added(self, tmp_path):
        """notes column is present after second init."""
        MetricsStore(db_path=tmp_path / "app.db")
        MetricsStore(db_path=tmp_path / "app.db")
        store = MetricsStore(db_path=tmp_path / "app.db")
        store.record(_metrics(notes="manual fix applied"))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.notes == "manual fix applied"

    def test_migrate_tags_and_notes_round_trip(self, tmp_path):
        """Both tags and notes survive a round-trip through the DB."""
        store = MetricsStore(db_path=tmp_path / "app.db")
        store.record(_metrics(tags='["rework"]', notes="operator note"))
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.tags == '["rework"]'
        assert result.notes == "operator note"

    def test_tags_and_notes_default_to_none(self, tmp_path):
        """New columns default to None for existing records."""
        store = MetricsStore(db_path=tmp_path / "app.db")
        store.record(_metrics())
        result = store.get_by_ticket("WOR-1", "proj-a")
        assert result is not None
        assert result.tags is None
        assert result.notes is None
