"""Tests for coexistence of MetricsStore and BenchStore in the same app.db."""

from __future__ import annotations

from pathlib import Path

from app.core.bench_store import BenchRun, BenchStore
from app.core.metrics import MetricsStore, TicketMetrics


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


class TestSimultaneousWrites:
    """Both stores write to the same app.db without conflict."""

    def test_both_stores_init_on_same_path(self, tmp_path: Path) -> None:
        """MetricsStore and BenchStore can both init on the same DB path."""
        db = tmp_path / "app.db"
        MetricsStore(db_path=db)
        BenchStore(db_path=db)
        assert db.exists()

    def test_metrics_write_does_not_break_bench_read(self, tmp_path: Path) -> None:
        """Writing metrics then reading bench data works without error."""
        db = tmp_path / "app.db"
        m_store = MetricsStore(db_path=db)
        b_store = BenchStore(db_path=db)

        m_store.record(_ticket(ticket_id="WOR-1"))
        results = b_store.get_by_run_id("nonexistent")
        assert results == []

    def test_bench_write_does_not_break_metrics_read(self, tmp_path: Path) -> None:
        """Writing bench data then reading metrics works without error."""
        db = tmp_path / "app.db"
        m_store = MetricsStore(db_path=db)
        b_store = BenchStore(db_path=db)

        b_store.record(BenchRun(run_id="r1", case_id="c1", repeat_index=0))
        result = m_store.get_by_ticket("WOR-99", "proj-a")
        assert result is None

    def test_concurrent_records_both_retrievable(self, tmp_path: Path) -> None:
        """Both stores can record and retrieve independently."""
        db = tmp_path / "app.db"
        m_store = MetricsStore(db_path=db)
        b_store = BenchStore(db_path=db)

        m_store.record(_ticket(ticket_id="WOR-1", project_id="proj-a"))
        b_store.record(BenchRun(run_id="r1", case_id="c1", repeat_index=0))

        m_result = m_store.get_by_ticket("WOR-1", "proj-a")
        assert m_result is not None
        assert m_result.ticket_id == "WOR-1"

        b_results = b_store.get_by_run_id("r1")
        assert len(b_results) == 1
        assert b_results[0].run_id == "r1"

    def test_multiple_tickets_and_runs_share_db(self, tmp_path: Path) -> None:
        """Multiple records from both stores coexist in the same file."""
        db = tmp_path / "app.db"
        m_store = MetricsStore(db_path=db)
        b_store = BenchStore(db_path=db)

        for i in range(3):
            m_store.record(_ticket(ticket_id=f"WOR-{i}", epic_id=f"WOR-{i}0"))
        for i in range(3):
            b_store.record(BenchRun(run_id=f"r{i}", case_id=f"c{i}", repeat_index=0))

        assert len(m_store.get_by_epic("WOR-10", "proj-a")) == 1
        assert len(b_store.get_by_run_id("r0")) == 1
        assert len(b_store.get_by_run_id("r2")) == 1

    def test_both_stores_create_tables_on_init(self, tmp_path: Path) -> None:
        """After both inits, the DB has all expected tables."""
        db = tmp_path / "app.db"
        MetricsStore(db_path=db)
        BenchStore(db_path=db)

        import sqlite3

        conn = sqlite3.connect(db)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "ticket_metrics" in tables
        assert "check_run_log" in tables
        assert "ticket_run_log" in tables
        assert "bench_run" in tables
