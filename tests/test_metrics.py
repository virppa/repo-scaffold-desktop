"""Tests for app.core.metrics — MetricsStore and TicketMetrics."""

from __future__ import annotations

import pytest

from app.core.metrics import (
    CheckRunEntry,
    CheckStats,
    EpicSummary,
    MetricsStore,
    TicketMetrics,
)


def _store(tmp_path) -> MetricsStore:
    return MetricsStore(db_path=tmp_path / "metrics.db")


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
        db = tmp_path / "metrics.db"
        assert not db.exists()
        MetricsStore(db_path=db)
        assert db.exists()

    def test_second_init_does_not_raise(self, tmp_path):
        MetricsStore(db_path=tmp_path / "metrics.db")
        MetricsStore(db_path=tmp_path / "metrics.db")


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
        failures = {"mypy": 2, "pytest": 1}
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
