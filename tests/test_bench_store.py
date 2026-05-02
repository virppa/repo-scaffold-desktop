"""Tests for app.core.bench_store — BenchStore and BenchRun."""

from __future__ import annotations

import sqlite3

import pytest

from app.core.bench_store import BenchRun, BenchStore, hash_settings, hash_text


def _store(tmp_path) -> BenchStore:
    return BenchStore(db_path=tmp_path / "bench.db")


def _bench_run(**kwargs) -> BenchRun:
    defaults: dict = {
        "run_id": "run-001",
        "case_id": "case-basic",
        "repeat_index": 0,
        "tier": "speed",
        "wall_time_s": 120.5,
        "quality_task_success": True,
    }
    defaults.update(kwargs)
    return BenchRun(**defaults)


class TestRoundTrip:
    def test_record_and_retrieve_by_run_id(self, tmp_path):
        store = _store(tmp_path)
        run = _bench_run(
            run_id="run-001",
            case_id="case-basic",
            repeat_index=0,
            tier="speed",
            wall_time_s=120.5,
            quality_task_success=True,
        )
        store.record(run)
        results = store.get_by_run_id("run-001")
        assert len(results) == 1
        assert results[0].run_id == "run-001"
        assert results[0].case_id == "case-basic"
        assert results[0].repeat_index == 0
        assert results[0].tier == "speed"
        assert results[0].wall_time_s == 120.5
        assert results[0].quality_task_success is True

    def test_record_and_retrieve_by_case_id(self, tmp_path):
        store = _store(tmp_path)
        run = _bench_run(
            run_id="run-002",
            case_id="case-unique",
            repeat_index=1,
            tier="accuracy",
        )
        store.record(run)
        results = store.get_by_case_id("case-unique")
        assert len(results) == 1
        assert results[0].run_id == "run-002"
        assert results[0].case_id == "case-unique"
        assert results[0].tier == "accuracy"


class TestPrimaryKeyConstraint:
    def test_duplicate_primary_key_raises(self, tmp_path):
        store = _store(tmp_path)
        run = _bench_run(run_id="dup-run", case_id="case-a", repeat_index=0)
        store.record(run)
        with pytest.raises(sqlite3.IntegrityError):
            store.record(run)


class TestBoolColumnSerialization:
    def test_bool_columns_serialized_as_int(self, tmp_path):
        store = _store(tmp_path)
        run = _bench_run(quality_pytest_passed=True)
        store.record(run)

        # Read raw int from SQLite
        conn = sqlite3.connect(str(tmp_path / "bench.db"))
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            "SELECT quality_pytest_passed FROM bench_run WHERE run_id = ?",
            ("run-001",),
        ).fetchone()
        conn.close()
        assert raw["quality_pytest_passed"] == 1

        # Read back through API — should be Python bool
        results = store.get_by_run_id("run-001")
        assert len(results) == 1
        assert results[0].quality_pytest_passed is True


class TestSchemaMigration:
    def test_schema_migration_is_idempotent(self, tmp_path):
        BenchStore(db_path=tmp_path / "bench.db")
        BenchStore(db_path=tmp_path / "bench.db")


class TestHashHelpers:
    def test_hash_settings_is_stable(self, tmp_path):
        a = hash_settings({"a": 1, "b": 2})
        b = hash_settings({"b": 2, "a": 1})
        assert a == b
        assert len(a) == 64  # SHA256 hex digest length

    def test_hash_text_is_stable(self, tmp_path):
        assert hash_text("hello") == hash_text("hello")
        h = hash_text("hello")
        assert len(h) == 64  # SHA256 hex digest length
