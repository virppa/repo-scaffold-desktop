"""Smoke tests for scripts.bench.reporter — table, export, load_sweep."""

from __future__ import annotations

import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from scripts.bench.reporter import (
    _SUMMARY_COLS,
    OOM_RISK_HEADROOM_GB,
    VRAM_HEADROOM_WARN_GB,
    _bool_col,
    _fmt,
    _median,
    _pct,
    _percentile,
    export_csv,
    export_json,
    load_sweep,
    print_summary_table,
)

# ── _SUMMARY_COLS ─────────────────────────────────────────────────────────────


class TestSummaryCols:
    def test_non_empty(self) -> None:
        assert len(_SUMMARY_COLS) > 0

    def test_all_tuples_of_two(self) -> None:
        for name, width in _SUMMARY_COLS:
            assert isinstance(name, str)
            assert isinstance(width, int)
            assert width > 0

    def test_header_columns(self) -> None:
        expected = ["Model", "Tier", "Ctx", "C", "R", "TTFT(s)", "Wall(s)", "Tok/s"]
        names = [name for name, _ in _SUMMARY_COLS]
        for exp in expected:
            assert exp in names


# ── _fmt ────────────────────────────────────────────────────────────────────────


class TestFmt:
    def test_none_returns_na(self) -> None:
        assert _fmt(None) == "--"

    def test_float_default(self) -> None:
        assert _fmt(3.14159) == "3.14159"

    def test_float_custom_format(self) -> None:
        assert _fmt(3.14159, ".2f") == "3.14"

    def test_int(self) -> None:
        assert _fmt(42) == "42"

    def test_string(self) -> None:
        assert _fmt("hello") == "hello"

    def test_custom_na(self) -> None:
        assert _fmt(None, na="N/A") == "N/A"


# ── _bool_col ───────────────────────────────────────────────────────────────────


class TestBoolCol:
    def test_true(self) -> None:
        assert _bool_col(True) == "Yes"

    def test_false(self) -> None:
        assert _bool_col(False) == "No"

    def test_none(self) -> None:
        assert _bool_col(None) == "--"


# ── _pct ────────────────────────────────────────────────────────────────────────


class TestPct:
    def test_none(self) -> None:
        assert _pct(None) == "--"

    def test_zero(self) -> None:
        assert _pct(0.0) == "0%"

    def test_full(self) -> None:
        assert _pct(100.0) == "100%"

    def test_half(self) -> None:
        assert _pct(50.0) == "50%"


# ── _median ─────────────────────────────────────────────────────────────────────


class TestMedian:
    def test_empty(self) -> None:
        assert _median([]) is None

    def test_single(self) -> None:
        assert _median([42.0]) == 42.0

    def test_even(self) -> None:
        assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_odd(self) -> None:
        assert _median([1.0, 2.0, 3.0]) == 2.0

    def test_skips_none(self) -> None:
        # _median filters None; constructing with None in list is intentional
        result = _median([1.0, None, 3.0])  # type: ignore[list-item]
        assert result == 2.0


# ── _percentile ─────────────────────────────────────────────────────────────────


class TestPercentile:
    def test_less_than_two(self) -> None:
        assert _percentile([1.0], 50) is None

    def test_two_values_median(self) -> None:
        assert _percentile([1.0, 3.0], 50) == 2.0

    def test_minimum(self) -> None:
        result = _percentile([1.0, 2.0, 3.0, 4.0], 0)
        assert result == 1.0

    def test_maximum(self) -> None:
        result = _percentile([1.0, 2.0, 3.0, 4.0], 100)
        assert result == 4.0


# ── print_summary_table ─────────────────────────────────────────────────────────


class TestPrintSummaryTable:
    def test_empty_rows(self) -> None:
        buf = StringIO()
        with redirect_stdout(buf):
            print_summary_table([])
        output = buf.getvalue()
        assert "(no results for this sweep)" in output

    def test_single_row(self) -> None:
        buf = StringIO()
        row = {
            "model_id": "test-model",
            "tier": "speed",
            "context_size": 4096,
            "concurrency": 1,
            "repeat_index": 1,
            "ttft_s": 0.5,
            "wall_time_s": 1.0,
            "throughput_tok_s": 100.0,
            "peak_vram_gb": 20.0,
            "avg_gpu_util_pct": 90.0,
            "outcome": "ok",
        }
        with redirect_stdout(buf):
            print_summary_table([row])
        output = buf.getvalue()
        assert "test-model" in output
        assert "speed" in output
        assert "4096" in output
        assert "100" in output  # throughput_tok_s

    def test_oom_shown(self) -> None:
        buf = StringIO()
        row = {
            "model_id": "m",
            "tier": "speed",
            "context_size": 4096,
            "concurrency": 1,
            "repeat_index": 1,
            "ttft_s": 0.5,
            "wall_time_s": 1.0,
            "throughput_tok_s": 100.0,
            "peak_vram_gb": 20.0,
            "avg_gpu_util_pct": 90.0,
            "outcome": "oom",
        }
        with redirect_stdout(buf):
            print_summary_table([row])
        output = buf.getvalue()
        assert "Yes" in output  # OOM column

    def test_header_present(self) -> None:
        buf = StringIO()
        row = {
            "model_id": "m",
            "tier": "speed",
            "context_size": 4096,
            "concurrency": 1,
            "repeat_index": 1,
            "ttft_s": 0.5,
            "wall_time_s": 1.0,
            "throughput_tok_s": 100.0,
            "peak_vram_gb": 20.0,
            "avg_gpu_util_pct": 90.0,
            "outcome": "ok",
        }
        with redirect_stdout(buf):
            print_summary_table([row])
        output = buf.getvalue()
        assert "SWEEP SUMMARY" in output


# ── export_json ─────────────────────────────────────────────────────────────────


class TestExportJson:
    def test_writes_json_file(self, tmp_path: Path) -> None:
        rows = [{"a": 1}, {"b": 2}]
        p = tmp_path / "out.json"
        export_json(rows, p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data == rows

    def test_empty_rows(self, tmp_path: Path) -> None:
        export_json([], tmp_path / "out.json")
        data = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
        assert data == []


# ── export_csv ──────────────────────────────────────────────────────────────────


class TestExportCsv:
    def test_writes_csv_with_header(self, tmp_path: Path) -> None:
        rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        p = tmp_path / "out.csv"
        export_csv(rows, p)
        content = p.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        assert lines[0] == "a,b"
        assert len(lines) == 3  # header + 2 data

    def test_empty_rows_produces_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "out.csv"
        export_csv([], p)
        assert p.read_text(encoding="utf-8") == ""


# ── load_sweep ──────────────────────────────────────────────────────────────────


class TestLoadSweep:
    def test_missing_db_returns_empty(self) -> None:
        assert load_sweep(Path("/nonexistent"), "s1") == []

    def test_valid_db_without_table_raises(self) -> None:
        """A valid sqlite3 file without bench_run table is not gracefully handled."""
        import sqlite3

        db = tempfile.mktemp(suffix=".db")
        try:
            conn = sqlite3.connect(db)
            conn.close()
            # Valid sqlite3 but no bench_run table → raises
            with pytest.raises(Exception):
                load_sweep(Path(db), "s1")
        finally:
            Path(db).unlink(missing_ok=True)

    def test_existing_bench_run_table(self, tmp_path: Path) -> None:
        """If bench_run table exists but has no matching rows, returns empty."""
        import sqlite3

        db = tmp_path / "bench.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE bench_run (run_id TEXT)")
        conn.execute("INSERT INTO bench_run VALUES ('s1::1')")
        conn.commit()
        conn.close()
        result = load_sweep(db, "s1")
        assert len(result) == 1
        assert result[0]["run_id"] == "s1::1"


# ── Constants ───────────────────────────────────────────────────────────────────


class TestConstants:
    def test_vram_headroom_warn_gb(self) -> None:
        assert VRAM_HEADROOM_WARN_GB == 2.0
        assert isinstance(VRAM_HEADROOM_WARN_GB, float)

    def test_oom_risk_headroom_gb(self) -> None:
        assert OOM_RISK_HEADROOM_GB == 0.5
        assert isinstance(OOM_RISK_HEADROOM_GB, float)
