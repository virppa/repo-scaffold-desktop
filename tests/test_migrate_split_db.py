"""Tests for the migration script that merges metrics.db + bench.db into app.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.migrate_split_db_to_unified import migrate


def _create_metrics_db(db_path: Path) -> None:
    """Create a legacy metrics.db with sample data."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE ticket_metrics (
            ticket_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            PRIMARY KEY (ticket_id, project_id)
        )
        """
    )
    conn.execute("INSERT INTO ticket_metrics VALUES ('WOR-1', 'proj-a', 'success')")
    conn.execute("INSERT INTO ticket_metrics VALUES ('WOR-2', 'proj-a', 'failure')")
    conn.execute(
        """
        CREATE TABLE check_run_log (
            id INTEGER PRIMARY KEY,
            ticket_id TEXT,
            check_cmd TEXT,
            outcome TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO check_run_log "
        "(id, ticket_id, check_cmd, outcome) "
        "VALUES (1, 'WOR-1', 'pytest', 'passed')"
    )
    conn.execute(
        """
        CREATE TABLE ticket_run_log (
            id INTEGER PRIMARY KEY,
            ticket_id TEXT,
            attempt INTEGER
        )
        """
    )
    conn.execute(
        "INSERT INTO ticket_run_log (id, ticket_id, attempt) VALUES (1, 'WOR-1', 1)"
    )
    conn.commit()
    conn.close()


def _create_bench_db(db_path: Path) -> None:
    """Create a legacy bench.db with sample data."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE bench_run (
            run_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            repeat_index INTEGER NOT NULL,
            outcome TEXT,
            PRIMARY KEY (run_id, case_id, repeat_index)
        )
        """
    )
    conn.execute("INSERT INTO bench_run VALUES ('r1', 'c1', 0, 'success')")
    conn.commit()
    conn.close()


class TestMigrationBasic:
    def test_migrates_metrics_tables(self, tmp_path: Path) -> None:
        metrics_db = tmp_path / "metrics.db"
        bench_db = tmp_path / "bench.db"
        target_db = tmp_path / "app.db"

        _create_metrics_db(metrics_db)

        migrate(metrics_db, bench_db, target_db)

        assert target_db.exists()
        with sqlite3.connect(target_db) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM ticket_metrics").fetchone()
            assert rows[0] == 2
            rows = conn.execute("SELECT COUNT(*) FROM check_run_log").fetchone()
            assert rows[0] == 1
            rows = conn.execute("SELECT COUNT(*) FROM ticket_run_log").fetchone()
            assert rows[0] == 1

    def test_migrates_bench_table(self, tmp_path: Path) -> None:
        metrics_db = tmp_path / "metrics.db"
        bench_db = tmp_path / "bench.db"
        target_db = tmp_path / "app.db"

        _create_bench_db(bench_db)

        migrate(metrics_db, bench_db, target_db)

        with sqlite3.connect(target_db) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM bench_run").fetchone()
            assert rows[0] == 1

    def test_handles_missing_legacy_files(self, tmp_path: Path) -> None:
        """Missing legacy files are skipped without error."""
        metrics_db = tmp_path / "metrics.db"
        bench_db = tmp_path / "bench.db"
        target_db = tmp_path / "app.db"

        # Neither file exists
        migrate(metrics_db, bench_db, target_db)

        # No DB created when source doesn't exist
        assert not target_db.exists()

    def test_idempotent_second_run(self, tmp_path: Path) -> None:
        """Running migration twice does not duplicate data."""
        metrics_db = tmp_path / "metrics.db"
        bench_db = tmp_path / "bench.db"
        target_db = tmp_path / "app.db"

        _create_metrics_db(metrics_db)
        _create_bench_db(bench_db)

        migrate(metrics_db, bench_db, target_db)
        migrate(metrics_db, bench_db, target_db)

        with sqlite3.connect(target_db) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM ticket_metrics").fetchone()
            assert rows[0] == 2

    def test_idempotent_noop_when_all_tables_exist(self, tmp_path: Path) -> None:
        """If app.db already has all tables, migration is a no-op."""
        metrics_db = tmp_path / "metrics.db"
        bench_db = tmp_path / "bench.db"
        target_db = tmp_path / "app.db"

        # Pre-create app.db with all tables
        target_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target_db)
        for table in ("ticket_metrics", "check_run_log", "ticket_run_log", "bench_run"):
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")  # nosemgrep
        conn.commit()
        conn.close()

        migrate(metrics_db, bench_db, target_db)

        # Should not have added any data
        with sqlite3.connect(target_db) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM ticket_metrics").fetchone()
            assert rows[0] == 0

    def test_uses_insert_or_ignore(self, tmp_path: Path) -> None:
        """Duplicate rows are silently skipped."""
        metrics_db = tmp_path / "metrics.db"
        bench_db = tmp_path / "bench.db"
        target_db = tmp_path / "app.db"

        _create_metrics_db(metrics_db)

        migrate(metrics_db, bench_db, target_db)
        migrate(metrics_db, bench_db, target_db)

        with sqlite3.connect(target_db) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM ticket_metrics").fetchone()
            assert rows[0] == 2  # Not 4

    def test_target_dir_created(self, tmp_path: Path) -> None:
        """Parent directory for app.db is created if missing."""
        metrics_db = tmp_path / "metrics.db"
        bench_db = tmp_path / "bench.db"
        target_db = tmp_path / "nested" / "dir" / "app.db"

        _create_metrics_db(metrics_db)

        migrate(metrics_db, bench_db, target_db)

        assert target_db.exists()
