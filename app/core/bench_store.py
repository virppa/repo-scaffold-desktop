"""Benchmark run data model and SQLite store.

SQLite-backed append-only store for benchmark run records.
Mirrors app/core/metrics.py: same _connect(), get_db_path(), DDL style,
and _APP_DIR='repo-scaffold', but uses _DB_NAME='bench.db'.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from pydantic import BaseModel, Field

_APP_DIR = "repo-scaffold"
_DB_NAME = "bench.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS bench_run (
    run_id                  TEXT    NOT NULL,
    case_id                 TEXT    NOT NULL,
    repeat_index            INTEGER NOT NULL,
    tier                    TEXT,
    context_size            INTEGER,
    concurrency             INTEGER,
    backend_id              TEXT,
    model_id                TEXT,
    settings_hash           TEXT,
    prompt_hash             TEXT,
    backend_base_url        TEXT,
    gpu_driver_version      TEXT,
    cuda_version            TEXT,
    python_version          TEXT,
    os_version              TEXT,
    ttft_s                  REAL,
    wall_time_s             REAL,
    throughput_tok_s        REAL,
    prompt_tokens           INTEGER,
    completion_tokens       INTEGER,
    total_tokens            INTEGER,
    peak_vram_gb            REAL,
    avg_gpu_util_pct        REAL,
    avg_gpu_mem_util_pct    REAL,
    avg_power_w             REAL,
    peak_temp_c             REAL,
    avg_sm_clock_mhz        REAL,
    avg_mem_clock_mhz       REAL,
    peak_ram_gb             REAL,
    cpu_offload_detected    INTEGER,
    ollama_model_loaded     INTEGER,
    ollama_num_ctx          INTEGER,
    quality_task_success    INTEGER,
    quality_pytest_passed   INTEGER,
    quality_ruff_passed     INTEGER,
    quality_mypy_passed     INTEGER,
    outcome                 TEXT,
    error_message           TEXT,
    recorded_at             TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, case_id, repeat_index)
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_bench_run_case_id
    ON bench_run (case_id)
"""

_INSERT = """
INSERT INTO bench_run (
    run_id, case_id, repeat_index,
    tier, context_size, concurrency, backend_id, model_id,
    settings_hash, prompt_hash,
    backend_base_url, gpu_driver_version, cuda_version, python_version, os_version,
    ttft_s, wall_time_s, throughput_tok_s,
    prompt_tokens, completion_tokens, total_tokens,
    peak_vram_gb, avg_gpu_util_pct, avg_gpu_mem_util_pct, avg_power_w,
    peak_temp_c, avg_sm_clock_mhz, avg_mem_clock_mhz,
    peak_ram_gb, cpu_offload_detected,
    ollama_model_loaded, ollama_num_ctx,
    quality_task_success, quality_pytest_passed,
    quality_ruff_passed, quality_mypy_passed,
    outcome, error_message
) VALUES (
    :run_id, :case_id, :repeat_index,
    :tier, :context_size, :concurrency, :backend_id, :model_id,
    :settings_hash, :prompt_hash,
    :backend_base_url, :gpu_driver_version, :cuda_version, :python_version, :os_version,
    :ttft_s, :wall_time_s, :throughput_tok_s,
    :prompt_tokens, :completion_tokens, :total_tokens,
    :peak_vram_gb, :avg_gpu_util_pct, :avg_gpu_mem_util_pct, :avg_power_w,
    :peak_temp_c, :avg_sm_clock_mhz, :avg_mem_clock_mhz,
    :peak_ram_gb, :cpu_offload_detected,
    :ollama_model_loaded, :ollama_num_ctx,
    :quality_task_success, :quality_pytest_passed,
    :quality_ruff_passed, :quality_mypy_passed,
    :outcome, :error_message
)
"""

_BOOL_COLUMNS = frozenset(
    {
        "cpu_offload_detected",
        "ollama_model_loaded",
        "quality_task_success",
        "quality_pytest_passed",
        "quality_ruff_passed",
        "quality_mypy_passed",
    }
)


class BenchRun(BaseModel):
    """Single benchmark run record."""

    model_config = {"extra": "forbid"}

    # identity — required
    run_id: str
    case_id: str
    repeat_index: int

    # config
    tier: str | None = None
    context_size: int | None = None
    concurrency: int | None = None
    backend_id: str | None = None
    model_id: str | None = None
    settings_hash: str | None = None
    prompt_hash: str | None = None

    # backend metadata
    backend_base_url: str | None = None
    gpu_driver_version: str | None = None
    cuda_version: str | None = None
    python_version: str | None = None
    os_version: str | None = None

    # timing
    ttft_s: float | None = Field(
        default=None, description="Time to first token in seconds"
    )
    wall_time_s: float | None = Field(
        default=None, description="Total wall time in seconds"
    )
    throughput_tok_s: float | None = Field(
        default=None, description="Tokens per second"
    )

    # tokens
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    # GPU
    peak_vram_gb: float | None = None
    avg_gpu_util_pct: float | None = None
    avg_gpu_mem_util_pct: float | None = None
    avg_power_w: float | None = None
    peak_temp_c: float | None = None
    avg_sm_clock_mhz: float | None = None
    avg_mem_clock_mhz: float | None = None

    # CPU/RAM
    peak_ram_gb: float | None = None
    cpu_offload_detected: bool | None = None

    # Ollama status
    ollama_model_loaded: bool | None = None
    ollama_num_ctx: int | None = None

    # quality
    quality_task_success: bool | None = None
    quality_pytest_passed: bool | None = None
    quality_ruff_passed: bool | None = None
    quality_mypy_passed: bool | None = None

    # outcome
    outcome: str | None = None
    error_message: str | None = None


def hash_settings(settings: dict[str, Any]) -> str:
    """Return stable SHA256 hex digest of JSON-serialized settings (key-sorted)."""
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def hash_text(text: str) -> str:
    """Return stable SHA256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode()).hexdigest()


class BenchStore:
    """SQLite-backed append-only store for benchmark run records."""

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
            conn.execute(_CREATE_INDEX)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, run: BenchRun) -> None:
        """Append a benchmark run record. Raises IntegrityError on duplicate run_id."""
        d = run.model_dump()
        for col in _BOOL_COLUMNS:
            if d[col] is not None:
                d[col] = int(d[col])
        with self._connect() as conn:
            conn.execute(_INSERT, d)

    def get_by_run_id(self, run_id: str) -> list[BenchRun]:
        """Return all BenchRun records for the given run_id, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bench_run WHERE run_id = ? ORDER BY recorded_at",
                (run_id,),
            ).fetchall()
        return [_row_to_bench_run(r) for r in rows]

    def get_by_case_id(self, case_id: str) -> list[BenchRun]:
        """Return all BenchRun records for a given case_id, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bench_run WHERE case_id = ? ORDER BY recorded_at",
                (case_id,),
            ).fetchall()
        return [_row_to_bench_run(r) for r in rows]


def _row_to_bench_run(row: sqlite3.Row) -> BenchRun:
    d = dict(row)
    d.pop("recorded_at", None)
    for col in _BOOL_COLUMNS:
        if d[col] is not None:
            d[col] = bool(d[col])
    return BenchRun.model_validate(d)
