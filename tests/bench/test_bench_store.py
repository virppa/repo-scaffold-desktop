"""Tests for app.core.bench_store — BenchRun model and BenchStore SQLite store."""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from app.core.bench_store import (
    BenchRun,
    BenchStore,
    hash_settings,
    hash_text,
)


def _store(tmp_path: Path) -> BenchStore:
    return BenchStore(db_path=tmp_path / "bench.db")


def _run(**kwargs: object) -> BenchRun:
    defaults: dict[str, object] = {
        "run_id": "run-001",
        "case_id": "case-a",
        "repeat_index": 1,
    }
    defaults.update(kwargs)
    return BenchRun(**defaults)  # type: ignore[arg-type]


class TestSchemaCreation:
    def test_db_file_created_on_init(self, tmp_path: Path) -> None:
        db = tmp_path / "bench.db"
        assert not db.exists()
        BenchStore(db_path=db)
        assert db.exists()

    def test_second_init_is_idempotent(self, tmp_path: Path) -> None:
        BenchStore(db_path=tmp_path / "bench.db")
        BenchStore(db_path=tmp_path / "bench.db")

    def test_index_exists_after_init(self, tmp_path: Path) -> None:
        import sqlite3

        db = tmp_path / "bench.db"
        BenchStore(db_path=db)
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='index' AND name='idx_bench_run_case_id'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1


class TestGetDbPath:
    def test_path_ends_with_bench_db(self) -> None:
        path = BenchStore.get_db_path()
        assert path.name == "bench.db"

    def test_path_is_in_platform_config_dir(self) -> None:
        path = BenchStore.get_db_path()
        if platform.system() == "Windows":
            assert "AppData" in str(path) or "Roaming" in str(path)
        else:
            assert ".config" in str(path)

    def test_path_contains_app_dir(self) -> None:
        path = BenchStore.get_db_path()
        assert "repo-scaffold" in str(path)


class TestRoundTrip:
    def test_minimal_run_round_trips(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.record(run)
        result = store.get_by_run_id("run-001")
        assert result is not None
        assert result.run_id == "run-001"
        assert result.case_id == "case-a"
        assert result.repeat_index == 1

    def test_all_none_optional_fields_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.record(run)
        result = store.get_by_run_id("run-001")
        assert result is not None
        assert result.tier is None
        assert result.context_size is None
        assert result.concurrency is None
        assert result.backend_id is None
        assert result.model_id is None
        assert result.settings_hash is None
        assert result.prompt_hash is None
        assert result.backend_base_url is None
        assert result.gpu_driver_version is None
        assert result.cuda_version is None
        assert result.python_version is None
        assert result.os_version is None
        assert result.ttft_s is None
        assert result.wall_time_s is None
        assert result.throughput_tok_s is None
        assert result.prompt_tokens is None
        assert result.completion_tokens is None
        assert result.total_tokens is None
        assert result.peak_vram_gb is None
        assert result.avg_gpu_util_pct is None
        assert result.avg_gpu_mem_util_pct is None
        assert result.avg_power_w is None
        assert result.peak_temp_c is None
        assert result.avg_sm_clock_mhz is None
        assert result.avg_mem_clock_mhz is None
        assert result.peak_ram_gb is None
        assert result.cpu_offload_detected is None
        assert result.ollama_model_loaded is None
        assert result.ollama_num_ctx is None
        assert result.quality_task_success is None
        assert result.quality_pytest_passed is None
        assert result.quality_ruff_passed is None
        assert result.quality_mypy_passed is None
        assert result.outcome is None
        assert result.error_message is None

    def test_full_run_round_trips(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(
            tier="speed",
            context_size=4096,
            concurrency=4,
            backend_id="local_a",
            model_id="qwen3-coder:30b",
            settings_hash="a" * 64,
            prompt_hash="b" * 64,
            backend_base_url="http://localhost:8082",
            gpu_driver_version="545.23",
            cuda_version="12.3",
            python_version="3.12.0",
            os_version="Windows-11",
            ttft_s=0.42,
            wall_time_s=12.5,
            throughput_tok_s=88.3,
            prompt_tokens=1024,
            completion_tokens=512,
            total_tokens=1536,
            peak_vram_gb=18.5,
            avg_gpu_util_pct=92.0,
            avg_gpu_mem_util_pct=85.0,
            avg_power_w=350.0,
            peak_temp_c=78.0,
            avg_sm_clock_mhz=2400.0,
            avg_mem_clock_mhz=10000.0,
            peak_ram_gb=32.1,
            cpu_offload_detected=False,
            ollama_model_loaded=True,
            ollama_num_ctx=4096,
            quality_task_success=True,
            quality_pytest_passed=True,
            quality_ruff_passed=True,
            quality_mypy_passed=False,
            outcome="success",
            error_message=None,
        )
        store.record(run)
        result = store.get_by_run_id("run-001")
        assert result is not None
        assert result == run

    def test_bool_fields_round_trip_true(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(
            cpu_offload_detected=True,
            ollama_model_loaded=True,
            quality_task_success=True,
            quality_pytest_passed=True,
            quality_ruff_passed=True,
            quality_mypy_passed=True,
        )
        store.record(run)
        result = store.get_by_run_id("run-001")
        assert result is not None
        assert result.cpu_offload_detected is True
        assert result.ollama_model_loaded is True
        assert result.quality_task_success is True
        assert result.quality_pytest_passed is True
        assert result.quality_ruff_passed is True
        assert result.quality_mypy_passed is True

    def test_bool_fields_round_trip_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(
            cpu_offload_detected=False,
            ollama_model_loaded=False,
            quality_task_success=False,
            quality_pytest_passed=False,
            quality_ruff_passed=False,
            quality_mypy_passed=False,
        )
        store.record(run)
        result = store.get_by_run_id("run-001")
        assert result is not None
        assert result.cpu_offload_detected is False
        assert result.ollama_model_loaded is False
        assert result.quality_task_success is False

    def test_missing_run_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_by_run_id("nonexistent") is None


class TestAppendOnly:
    def test_duplicate_run_id_raises(self, tmp_path: Path) -> None:
        import sqlite3 as _sqlite3

        store = _store(tmp_path)
        store.record(_run())
        with pytest.raises(_sqlite3.IntegrityError):
            store.record(_run())

    def test_multiple_runs_accumulate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run(run_id="run-1"))
        store.record(_run(run_id="run-2"))
        store.record(_run(run_id="run-3"))
        assert store.get_by_run_id("run-1") is not None
        assert store.get_by_run_id("run-2") is not None
        assert store.get_by_run_id("run-3") is not None


class TestGetByCaseId:
    def test_returns_all_runs_for_case(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run(run_id="r1", case_id="case-x", repeat_index=0))
        store.record(_run(run_id="r2", case_id="case-x", repeat_index=1))
        store.record(_run(run_id="r3", case_id="case-y", repeat_index=1))
        results = store.get_by_case_id("case-x")
        assert len(results) == 2
        assert {r.run_id for r in results} == {"r1", "r2"}

    def test_unknown_case_returns_empty_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_by_case_id("no-such-case") == []


class TestHashFunctions:
    def test_hash_settings_is_64_chars(self) -> None:
        h = hash_settings({"temperature": 0.7, "max_tokens": 2048})
        assert len(h) == 64

    def test_hash_settings_stable(self) -> None:
        settings = {"temperature": 0.7, "seed": 42}
        assert hash_settings(settings) == hash_settings(settings)

    def test_hash_settings_key_order_independent(self) -> None:
        assert hash_settings({"a": 1, "b": 2}) == hash_settings({"b": 2, "a": 1})

    def test_hash_settings_different_values_differ(self) -> None:
        assert hash_settings({"a": 1}) != hash_settings({"a": 2})

    def test_hash_settings_empty_dict_stable(self) -> None:
        h1 = hash_settings({})
        h2 = hash_settings({})
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_text_is_64_chars(self) -> None:
        h = hash_text("hello world")
        assert len(h) == 64

    def test_hash_text_stable(self) -> None:
        assert hash_text("same") == hash_text("same")

    def test_hash_text_different_inputs_differ(self) -> None:
        assert hash_text("foo") != hash_text("bar")
