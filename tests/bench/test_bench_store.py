"""Tests for app.core.bench_store — BenchRun model and BenchStore SQLite store."""

from __future__ import annotations

import io
import platform
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from app.core.bench_store import (
    BenchRun,
    BenchStore,
    hash_settings,
    hash_text,
)
from scripts.bench.reporter import print_ranking, print_summary_table


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
        results = store.get_by_run_id("run-001")
        assert len(results) == 1
        result = results[0]
        assert result.run_id == "run-001"
        assert result.case_id == "case-a"
        assert result.repeat_index == 1

    def test_all_none_optional_fields_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.record(run)
        results = store.get_by_run_id("run-001")
        assert len(results) == 1
        result = results[0]
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
        results = store.get_by_run_id("run-001")
        assert len(results) == 1
        assert results[0] == run

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
        results = store.get_by_run_id("run-001")
        assert len(results) == 1
        result = results[0]
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
        results = store.get_by_run_id("run-001")
        assert len(results) == 1
        result = results[0]
        assert result.cpu_offload_detected is False
        assert result.ollama_model_loaded is False
        assert result.quality_task_success is False

    def test_missing_run_returns_empty_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_by_run_id("nonexistent") == []


class TestAppendOnly:
    def test_duplicate_run_case_repeat_raises(self, tmp_path: Path) -> None:
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
        assert len(store.get_by_run_id("run-1")) == 1
        assert len(store.get_by_run_id("run-2")) == 1
        assert len(store.get_by_run_id("run-3")) == 1


class TestGetByRunId:
    def test_returns_all_cases_for_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run(run_id="batch-1", case_id="case-a", repeat_index=0))
        store.record(_run(run_id="batch-1", case_id="case-a", repeat_index=1))
        store.record(_run(run_id="batch-1", case_id="case-b", repeat_index=0))
        store.record(_run(run_id="batch-2", case_id="case-a", repeat_index=0))
        results = store.get_by_run_id("batch-1")
        assert len(results) == 3
        assert all(r.run_id == "batch-1" for r in results)
        assert {(r.case_id, r.repeat_index) for r in results} == {
            ("case-a", 0),
            ("case-a", 1),
            ("case-b", 0),
        }

    def test_unknown_run_returns_empty_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_by_run_id("no-such-run") == []


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


class TestNewColumns:
    """Tests for the 5 new BenchRun columns added in WOR-197."""

    def test_new_columns_default_to_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run()
        store.record(run)
        result = store.get_by_run_id("run-001")[0]
        assert result.prompt_eval_duration_s is None
        assert result.load_duration_s is None
        assert result.decode_time_s is None
        assert result.cache_state is None
        assert result.total_vram_gb is None

    def test_new_columns_round_trip_with_values(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        run = _run(
            prompt_eval_duration_s=0.1,
            load_duration_s=2.5,
            decode_time_s=0.5,
            cache_state="warm",
            total_vram_gb=24.0,
        )
        store.record(run)
        result = store.get_by_run_id("run-001")[0]
        assert result.prompt_eval_duration_s == pytest.approx(0.1)
        assert result.load_duration_s == pytest.approx(2.5)
        assert result.decode_time_s == pytest.approx(0.5)
        assert result.cache_state == "warm"
        assert result.total_vram_gb == pytest.approx(24.0)

    def test_migration_adds_columns_to_existing_db(self, tmp_path: Path) -> None:
        """An existing bench.db without new columns opens cleanly after migration."""
        db_path = tmp_path / "old.db"
        # Build an old-schema DB without the new columns
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE bench_run (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                repeat_index INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (run_id, case_id, repeat_index)
            )
            """
        )
        conn.execute(
            "INSERT INTO bench_run(run_id, case_id, repeat_index) VALUES ('r1','c1',0)"
        )
        conn.commit()
        conn.close()

        # Opening with BenchStore should migrate silently
        store = BenchStore(db_path=db_path)
        results = store.get_by_run_id("r1")
        assert len(results) == 1
        assert results[0].cache_state is None
        assert results[0].total_vram_gb is None

    def test_cache_state_prefix_warm_round_trips(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run(run_id="r1", cache_state="prefix_warm"))
        result = store.get_by_run_id("r1")[0]
        assert result.cache_state == "prefix_warm"


class TestModelMetadataColumns:
    """Tests for model_quant and model_family added in WOR-207."""

    def test_model_quant_and_family_default_to_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run())
        result = store.get_by_run_id("run-001")[0]
        assert result.model_quant is None
        assert result.model_family is None

    def test_model_quant_and_family_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run(model_quant="Q4_K_M", model_family="30.5B"))
        result = store.get_by_run_id("run-001")[0]
        assert result.model_quant == "Q4_K_M"
        assert result.model_family == "30.5B"

    def test_migration_adds_model_quant_and_family_columns(
        self, tmp_path: Path
    ) -> None:
        import sqlite3

        db_path = tmp_path / "old_schema.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE bench_run (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                repeat_index INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (run_id, case_id, repeat_index)
            )
            """
        )
        conn.execute(
            "INSERT INTO bench_run(run_id, case_id, repeat_index) VALUES ('r1','c1',0)"
        )
        conn.commit()
        conn.close()

        store = BenchStore(db_path=db_path)
        result = store.get_by_run_id("r1")[0]
        assert result.model_quant is None
        assert result.model_family is None


class TestModelParamCountColumn:
    """Tests for model_param_count TEXT column added in WOR-207."""

    def test_model_param_count_defaults_to_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run())
        result = store.get_by_run_id("run-001")[0]
        assert result.model_param_count is None

    def test_model_param_count_round_trips(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record(_run(model_param_count="30.5B"))
        result = store.get_by_run_id("run-001")[0]
        assert result.model_param_count == "30.5B"

    def test_migration_adds_model_param_count_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "old_schema.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE bench_run (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                repeat_index INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (run_id, case_id, repeat_index)
            )
            """
        )
        conn.execute(
            "INSERT INTO bench_run(run_id, case_id, repeat_index) VALUES ('r1','c1',0)"
        )
        conn.commit()
        conn.close()

        store = BenchStore(db_path=db_path)
        result = store.get_by_run_id("r1")[0]
        assert result.model_param_count is None

    def test_bench_run_field_is_str_or_none(self) -> None:
        run = _run(model_param_count="7B")
        assert run.model_param_count == "7B"
        run2 = _run()
        assert run2.model_param_count is None


class TestPrintRanking:
    """Tests for per-config grouping in print_ranking()."""

    def _make_row(
        self,
        backend_id: str,
        model_id: str,
        context_size: int,
        concurrency: int,
        repeat_index: int = 1,
        ttft_s: float = 0.3,
        throughput_tok_s: float = 80.0,
        outcome: str = "ok",
        cpu_offload_detected: bool = False,
    ) -> dict:
        return {
            "backend_id": backend_id,
            "model_id": model_id,
            "context_size": context_size,
            "concurrency": concurrency,
            "repeat_index": repeat_index,
            "ttft_s": ttft_s,
            "throughput_tok_s": throughput_tok_s,
            "outcome": outcome,
            "cpu_offload_detected": cpu_offload_detected,
            "tier": "speed",
            "quality_task_success": None,
        }

    def _capture_ranking(self, rows: list[dict]) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_ranking(rows)
        return buf.getvalue()

    def test_same_model_different_ctx_produces_two_rows(self) -> None:
        rows = [
            self._make_row("b", "m", 4096, 1, ttft_s=0.2),
            self._make_row("b", "m", 8192, 1, ttft_s=0.4),
        ]
        output = self._capture_ranking(rows)
        assert "ctx=4096" in output
        assert "ctx=8192" in output
        # Both configs should appear as separate ranked entries
        assert output.count("   1") >= 1
        assert output.count("   2") >= 1

    def test_same_model_different_concurrency_produces_two_rows(self) -> None:
        rows = [
            self._make_row("b", "m", 4096, 1, ttft_s=0.2),
            self._make_row("b", "m", 4096, 4, ttft_s=0.5),
        ]
        output = self._capture_ranking(rows)
        assert "c=1" in output
        assert "c=4" in output

    def test_best_ttft_ranks_first(self) -> None:
        rows = [
            self._make_row("b", "m", 4096, 1, ttft_s=0.5),
            self._make_row("b", "m", 8192, 1, ttft_s=0.2),
        ]
        output = self._capture_ranking(rows)
        idx_4096 = output.find("ctx=4096")
        idx_8192 = output.find("ctx=8192")
        # ctx=8192 has lower TTFT so it should appear first (higher up in output)
        assert idx_8192 < idx_4096

    def test_no_eligible_rows_prints_message(self) -> None:
        rows = [
            self._make_row("b", "m", 4096, 1, outcome="oom"),
        ]
        output = self._capture_ranking(rows)
        assert "No quality-eligible" in output

    def test_recommended_banner_shows_config_key(self) -> None:
        rows = [self._make_row("local", "qwen3:30b", 4096, 2)]
        output = self._capture_ranking(rows)
        assert "RECOMMENDED" in output
        assert "local/qwen3:30b" in output


class TestThermalThrottleColumns:
    """Tests for min_sm_clock_mhz and thermal_throttle_detected added in WOR-205."""

    def test_new_columns_default_to_none(self, tmp_path: Path) -> None:
        store = BenchStore(db_path=tmp_path / "bench.db")
        store.record(_run())
        result = store.get_by_run_id("run-001")[0]
        assert result.min_sm_clock_mhz is None
        assert result.thermal_throttle_detected is None

    def test_min_sm_clock_mhz_round_trips(self, tmp_path: Path) -> None:
        store = BenchStore(db_path=tmp_path / "bench.db")
        store.record(_run(min_sm_clock_mhz=1200.0))
        result = store.get_by_run_id("run-001")[0]
        assert result.min_sm_clock_mhz == pytest.approx(1200.0)

    def test_thermal_throttle_detected_true_round_trips(self, tmp_path: Path) -> None:
        store = BenchStore(db_path=tmp_path / "bench.db")
        store.record(_run(thermal_throttle_detected=True))
        result = store.get_by_run_id("run-001")[0]
        assert result.thermal_throttle_detected is True

    def test_thermal_throttle_detected_false_round_trips(self, tmp_path: Path) -> None:
        store = BenchStore(db_path=tmp_path / "bench.db")
        store.record(_run(thermal_throttle_detected=False))
        result = store.get_by_run_id("run-001")[0]
        assert result.thermal_throttle_detected is False

    def test_migration_adds_thermal_throttle_columns(self, tmp_path: Path) -> None:
        import sqlite3

        db_path = tmp_path / "old_schema.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE bench_run (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                repeat_index INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (run_id, case_id, repeat_index)
            )
            """
        )
        conn.execute(
            "INSERT INTO bench_run(run_id, case_id, repeat_index) VALUES ('r1','c1',0)"
        )
        conn.commit()
        conn.close()

        store = BenchStore(db_path=db_path)
        result = store.get_by_run_id("r1")[0]
        assert result.min_sm_clock_mhz is None
        assert result.thermal_throttle_detected is None


class TestTtfutColumn:
    """Tests for ttfut_s added in WOR-202."""

    def test_ttfut_s_defaults_to_none(self, tmp_path: Path) -> None:
        store = BenchStore(db_path=tmp_path / "bench.db")
        store.record(_run())
        result = store.get_by_run_id("run-001")[0]
        assert result.ttfut_s is None

    def test_ttfut_s_round_trips(self, tmp_path: Path) -> None:
        store = BenchStore(db_path=tmp_path / "bench.db")
        store.record(_run(ttfut_s=1.234))
        result = store.get_by_run_id("run-001")[0]
        assert result.ttfut_s == pytest.approx(1.234)

    def test_migration_adds_ttfut_s_column(self, tmp_path: Path) -> None:
        import sqlite3

        db_path = tmp_path / "old_schema.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE bench_run (
                run_id TEXT NOT NULL, case_id TEXT NOT NULL,
                repeat_index INTEGER NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (run_id, case_id, repeat_index)
            )
            """
        )
        conn.execute(
            "INSERT INTO bench_run(run_id, case_id, repeat_index) VALUES ('r1','c1',0)"
        )
        conn.commit()
        conn.close()

        store = BenchStore(db_path=db_path)
        result = store.get_by_run_id("r1")[0]
        assert result.ttfut_s is None

    def test_summary_table_shows_ttfut_column_when_present(self) -> None:
        rows = [{"model_id": "qwen3:30b", "ttfut_s": 1.5, "ttft_s": 0.2}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary_table(rows)
        assert "TTFUT" in buf.getvalue()

    def test_summary_table_hides_ttfut_column_when_all_none(self) -> None:
        rows = [{"model_id": "mistral", "ttfut_s": None, "ttft_s": 0.2}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary_table(rows)
        assert "TTFUT" not in buf.getvalue()


class TestThrottleColumn:
    """Tests for the conditional Throttle column in print_summary_table (WOR-205)."""

    def test_throttle_column_shown_when_any_row_throttled(self) -> None:
        rows = [
            {"model_id": "m", "thermal_throttle_detected": True},
            {"model_id": "m2", "thermal_throttle_detected": False},
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary_table(rows)
        assert "Throttle" in buf.getvalue()

    def test_throttle_column_hidden_when_no_row_throttled(self) -> None:
        rows = [
            {"model_id": "m", "thermal_throttle_detected": False},
            {"model_id": "m2", "thermal_throttle_detected": None},
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary_table(rows)
        assert "Throttle" not in buf.getvalue()

    def test_throttle_column_hidden_when_all_none(self) -> None:
        rows = [{"model_id": "m", "thermal_throttle_detected": None}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_summary_table(rows)
        assert "Throttle" not in buf.getvalue()
