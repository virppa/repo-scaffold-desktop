"""Benchmark runner CLI entry point.

Usage:
    python scripts/bench/run_bench.py --tier speed
    python scripts/bench/run_bench.py --resume run_20240101_120000
    python scripts/bench/run_bench.py --compare run_20240101 run_20240102
    python scripts/bench/run_bench.py --generate-fixtures
    python scripts/bench/run_bench.py --browse
"""

from __future__ import annotations

import argparse
import logging
import platform
import sqlite3
import subprocess  # nosec B404
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from scripts.bench.config import BenchCase, BenchConfig
from scripts.bench.drivers.ollama import OllamaDriver
from scripts.bench.drivers.vllm import VllmDriver
from scripts.bench.env_snapshot import EnvSnapshot
from scripts.bench.gpu_monitor import GpuMonitor
from scripts.bench.lifecycle.ollama_manager import OllamaManager
from scripts.bench.quality import evaluate_coding_output
from scripts.bench.sys_monitor import SysMonitor
from scripts.bench.tasks.boundary import make_boundary_prompt
from scripts.bench.tasks.coding import make_coding_prompt
from scripts.bench.tasks.prefill_shared import make_prefill_shared_prompt
from scripts.bench.tasks.prefill_unshared import make_prefill_unshared_prompt
from scripts.bench.tasks.speed import make_speed_prompt

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# ── DB constants (mirrors app/core/bench_store.py — no app.* imports allowed) ─

_APP_DIR = "repo-scaffold"
_DB_NAME = "bench.db"
_FIXTURES_DIR = Path(__file__).parent / "fixtures"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS bench_run (
    run_id                  TEXT    NOT NULL PRIMARY KEY,
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
    recorded_at             TEXT NOT NULL DEFAULT (datetime('now'))
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

# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_db_path() -> Path:
    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming"
    else:
        base = Path.home() / ".config"
    return base / _APP_DIR / _DB_NAME


@contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)


def _run_id_exists(db_path: Path, run_id: str) -> bool:
    with _connect(db_path) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM bench_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            is not None
        )


def _insert_row(db_path: Path, row: dict[str, Any]) -> None:
    with _connect(db_path) as conn:
        conn.execute(_INSERT, row)


# ── ID helpers ────────────────────────────────────────────────────────────────


def _case_id(case: BenchCase) -> str:
    return (
        f"{case.backend_id}/{case.model_id}"
        f"/{case.tier}/{case.context_size}/{case.concurrency}"
    )


def _row_run_id(sweep_id: str, case: BenchCase) -> str:
    return f"{sweep_id}::{_case_id(case)}::{case.repeat_index}"


# ── OOM detection ─────────────────────────────────────────────────────────────


def _is_oom(error: str) -> bool:
    low = error.lower()
    return (
        "507" in error
        or "out of memory" in low
        or "connection reset" in low
        or "connectionreset" in low
    )


# ── Prompt factory ────────────────────────────────────────────────────────────


def _make_prompt(tier: str, repeat_index: int) -> Any:
    if tier == "speed":
        return make_speed_prompt()
    if tier == "coding":
        return make_coding_prompt()
    if tier == "prefill_shared":
        return make_prefill_shared_prompt(suffix_index=repeat_index)
    if tier == "prefill_unshared":
        return make_prefill_unshared_prompt()
    if tier == "boundary":
        return make_boundary_prompt()
    raise ValueError(f"Unknown tier: {tier!r}")


# ── Driver factory ────────────────────────────────────────────────────────────


def _make_driver(backend_id: str, base_url: str) -> OllamaDriver | VllmDriver:
    if "vllm" in backend_id.lower():
        return VllmDriver(base_url=base_url)
    return OllamaDriver(base_url=base_url)


# ── Fixture generation ────────────────────────────────────────────────────────

_PROJECT_SUMMARY_TEMPLATE = """\
# Project Summary: repo-scaffold-desktop

## Overview

repo-scaffold-desktop is a desktop application for generating repository
scaffolds from configurable presets. It supports Jinja2 templates, optional
git initialization, pre-commit hook installation, CI workflow generation,
and CODEOWNERS configuration. The application is built with PySide6 for the
GUI and exposes a CLI for automation.

## Architecture

The codebase follows a strict layered architecture:

- **app/core/** — all business logic; no UI code allowed here
- **app/ui/** — PySide6 presentation layer; calls core, contains no logic
- **templates/** — Jinja2 template files rendered during scaffold generation
- **tests/** — unit tests for core logic only
- **schemas/** — exported JSON Schemas for non-Python consumers
- **scripts/bench/** — standalone benchmark suite; no app.* imports allowed

Data flows one way: UI → config model → generator → disk.

## Module Responsibilities

- **config.py** — Pydantic input models (repo name, output path, preset)
- **presets.py** — preset definitions (maps preset name → file list)
- **generator.py** — renders templates and writes files to disk
- **post_setup.py** — side effects: git init, pre-commit install, etc.
- **user_prefs.py** — UserPreferences model and PrefsStore (JSON persistence)
- **manifest.py** — ExecutionManifest Pydantic model: cloud→local contract
- **escalation_policy.py** — EscalationPolicy: loads escalation_policy.toml
- **linear_client.py** — thin Linear GraphQL client (stdlib urllib only)
- **metrics.py** — SQLite-backed store for per-ticket cost and metrics
- **bench_store.py** — SQLite store for benchmark run records (GPU, timing)

## Engineering Principles

1. UI stays thin. No branching logic, no file I/O in app/ui/.
2. Prefer config + templates over conditional generation logic.
3. Generated output must be deterministic and easy to diff.
4. Avoid over-abstracting v1. Three similar lines beat a premature helper.
5. Side effects (git, pre-commit) live only in post_setup.py.
6. Architecture contracts are enforced by Import Linter.

## Benchmark Suite

The benchmark suite (scripts/bench/) evaluates local LLM backends:

- **speed** — minimal prompt measuring raw generation latency
- **coding** — coding task with automated quality evaluation
- **prefill_shared** — long shared document prefix to test KV-cache reuse
- **prefill_unshared** — fresh random document each run (cold prefill)
- **boundary** — context-window edge probing

The runner collects GPU metrics (VRAM, utilization, power, temperature,
SM/memory clocks), system metrics (RAM, CPU offload detection), timing
metrics (TTFT, wall time, throughput), and quality metrics for coding tasks.

## Development Workflow

Each ticket follows grooming → planning → local implementation → PR phases.
The watcher daemon polls Linear for ReadyForLocal tickets and orchestrates
local worker sessions in isolated git worktrees.

"""


def _generate_fixtures() -> None:
    """Create scripts/bench/fixtures/project_summary_50k.txt (~50k tokens)."""
    _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    target = _FIXTURES_DIR / "project_summary_50k.txt"
    # Target: ~50k tokens ≈ 200k chars at 4 chars/token
    target_chars = 200_000
    base = _PROJECT_SUMMARY_TEMPLATE
    # Repeat sections with slight variation to reach target size
    parts = [base]
    section_idx = 0
    while sum(len(p) for p in parts) < target_chars:
        section_idx += 1
        parts.append(
            f"\n\n## Extended Notes — Section {section_idx}\n\n"
            + base.replace(
                "repo-scaffold-desktop", f"repo-scaffold-desktop (ref {section_idx})"
            )
        )
    content = "".join(parts)[:target_chars]
    target.write_text(content, encoding="utf-8")
    print(f"Fixture written: {target} ({len(content):,} chars)")


# ── Browse ────────────────────────────────────────────────────────────────────


def _browse(db_path: Path) -> None:
    """Open bench.db in datasette."""
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    try:
        subprocess.run(  # nosec B603
            [sys.executable, "-m", "datasette", str(db_path)],
            check=True,
        )
    except FileNotFoundError:
        print(
            "datasette is not installed. Install with: pip install datasette",
            file=sys.stderr,
        )
        sys.exit(1)


# ── Compare ───────────────────────────────────────────────────────────────────


def _compare(id1: str, id2: str, db_path: Path) -> None:
    from scripts.bench import reporter

    rows1 = reporter.load_sweep(db_path, id1)
    rows2 = reporter.load_sweep(db_path, id2)
    reporter.print_compare_table(rows1, rows2, id1, id2)


# ── Main runner ───────────────────────────────────────────────────────────────


def _run(args: argparse.Namespace, db_path: Path) -> None:
    config = BenchConfig.from_toml(args.config)
    cases = config.expand_matrix()

    if args.tier:
        cases = [c for c in cases if c.tier == args.tier]

    sweep_id: str = (
        args.resume
        if args.resume
        else f"run_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )
    print(f"Sweep: {sweep_id}  DB: {db_path}")

    _ensure_schema(db_path)

    backends = {b.id: b for b in config.backends if b.enabled}

    prev_model_id: str | None = None
    prev_backend_id: str | None = None
    # Tracks which models have had at least one prefill_shared run (for cache_state)
    prefill_shared_seen: set[str] = set()

    for case in cases:
        row_run_id = _row_run_id(sweep_id, case)

        if args.resume and _run_id_exists(db_path, row_run_id):
            print(
                f"[SKIP] {case.model_id}/{case.tier}"
                f"/ctx={case.context_size}/c={case.concurrency}/r={case.repeat_index}"
            )
            continue

        backend_cfg = backends.get(case.backend_id)
        if backend_cfg is None:
            print(
                f"[WARN] Unknown backend {case.backend_id!r}, skipping",
                file=sys.stderr,
            )
            continue

        driver = _make_driver(case.backend_id, backend_cfg.base_url)

        manager: OllamaManager | None = None
        if "vllm" not in case.backend_id.lower():
            manager = OllamaManager(base_url=backend_cfg.base_url)
            try:
                manager.ensure_running()
                manager.pull_if_needed(case.model_id)
            except TimeoutError as exc:
                print(f"[ERROR] Ollama not ready: {exc}", file=sys.stderr)
                continue

        # Flush previous model when switching models within a backend
        if (
            manager is not None
            and prev_model_id is not None
            and (prev_model_id != case.model_id or prev_backend_id != case.backend_id)
        ):
            try:
                manager.flush_model(prev_model_id)
            except Exception as exc:
                logging.warning("flush_model failed: %s", exc)

        prev_model_id = case.model_id
        prev_backend_id = case.backend_id

        settings: dict[str, Any] = {
            "tier": case.tier,
            "context_size": case.context_size,
            "concurrency": case.concurrency,
        }
        env = EnvSnapshot.capture(
            backend=case.backend_id, model=case.model_id, settings=settings
        )

        # First prefill_shared per model = warm; subsequent = prefix_warm
        cache_state: str | None = None
        if case.tier == "prefill_shared":
            key = f"{case.backend_id}/{case.model_id}"
            if key not in prefill_shared_seen:
                prefill_shared_seen.add(key)
                cache_state = "warm"
            else:
                cache_state = "prefix_warm"

        prompt = _make_prompt(case.tier, case.repeat_index)

        gpu_mon = GpuMonitor()
        sys_mon = SysMonitor()
        gpu_mon.start()
        sys_mon.start()

        messages: list[dict[str, str]] = [{"role": "user", "content": prompt.text}]
        t_start = time.monotonic()
        result = driver.generate(case.model_id, messages)
        wall_time_s = time.monotonic() - t_start

        gpu_sample = gpu_mon.stop()
        sys_result = sys_mon.stop()

        # OOM detection
        oom = False
        outcome = "ok"
        error_message: str | None = None
        if result.error:
            error_message = result.error
            if _is_oom(result.error):
                oom = True
                outcome = "oom"
            else:
                outcome = "error"

        # Quality evaluation for coding tier only
        quality_task_success: bool | None = None
        quality_pytest_passed: bool | None = None
        quality_ruff_passed: bool | None = None
        quality_mypy_passed: bool | None = None
        if case.tier == "coding" and not oom and result.text:
            repo_path = Path(__file__).parent.parent.parent
            qr = evaluate_coding_output(result.text, repo_path)
            quality_task_success = qr.task_success
            quality_pytest_passed = qr.pytest_passed
            quality_ruff_passed = qr.ruff_passed
            quality_mypy_passed = qr.mypy_passed
            if qr.error_message and error_message is None:
                error_message = qr.error_message

        total_tokens: int | None = None
        if result.input_tokens is not None and result.output_tokens is not None:
            total_tokens = result.input_tokens + result.output_tokens

        throughput_tok_s: float | None = None
        if (
            result.output_tokens
            and result.decode_time_s is not None
            and result.decode_time_s > 0
        ):
            throughput_tok_s = result.output_tokens / result.decode_time_s

        ollama_model_loaded: bool | None = None
        if manager is not None:
            try:
                statuses = manager.get_ps_status()
                ollama_model_loaded = any(s.name == case.model_id for s in statuses)
            except Exception:
                pass

        row: dict[str, Any] = {
            "run_id": row_run_id,
            "case_id": _case_id(case),
            "repeat_index": case.repeat_index,
            "tier": case.tier,
            "context_size": case.context_size,
            "concurrency": case.concurrency,
            "backend_id": case.backend_id,
            "model_id": case.model_id,
            "settings_hash": env.settings_hash,
            "prompt_hash": prompt.prompt_hash,
            "backend_base_url": backend_cfg.base_url,
            "gpu_driver_version": env.gpu_driver_version,
            "cuda_version": env.cuda_version,
            "python_version": env.python_version,
            "os_version": env.os_version,
            "ttft_s": result.ttft_s if not oom else None,
            "wall_time_s": wall_time_s if not oom else None,
            "throughput_tok_s": throughput_tok_s,
            "prompt_tokens": result.input_tokens,
            "completion_tokens": result.output_tokens,
            "total_tokens": total_tokens,
            "peak_vram_gb": gpu_sample.peak_vram_gb,
            "avg_gpu_util_pct": gpu_sample.avg_gpu_util_pct,
            "avg_gpu_mem_util_pct": gpu_sample.avg_gpu_mem_util_pct,
            "avg_power_w": gpu_sample.avg_power_w,
            "peak_temp_c": gpu_sample.peak_temp_c,
            "avg_sm_clock_mhz": gpu_sample.avg_sm_clock_mhz,
            "avg_mem_clock_mhz": gpu_sample.avg_mem_clock_mhz,
            "peak_ram_gb": sys_result.peak_ram_gb,
            "cpu_offload_detected": int(sys_result.cpu_offload_detected),
            "ollama_model_loaded": (
                int(ollama_model_loaded) if ollama_model_loaded is not None else None
            ),
            "ollama_num_ctx": case.context_size,
            "quality_task_success": (
                int(quality_task_success) if quality_task_success is not None else None
            ),
            "quality_pytest_passed": (
                int(quality_pytest_passed)
                if quality_pytest_passed is not None
                else None
            ),
            "quality_ruff_passed": (
                int(quality_ruff_passed) if quality_ruff_passed is not None else None
            ),
            "quality_mypy_passed": (
                int(quality_mypy_passed) if quality_mypy_passed is not None else None
            ),
            "outcome": outcome,
            "error_message": error_message,
        }

        # Write before moving to next case (resume safety invariant)
        _insert_row(db_path, row)

        ttft_str = f" ttft={result.ttft_s:.2f}s" if result.ttft_s else ""
        tok_str = f" tok/s={throughput_tok_s:.0f}" if throughput_tok_s else ""
        cache_str = f" [{cache_state}]" if cache_state else ""
        print(
            f"[{outcome.upper():5}] {case.model_id} / {case.tier}"
            f" / ctx={case.context_size} / c={case.concurrency} / r={case.repeat_index}"
            f"{cache_str}{ttft_str}{tok_str}"
        )

    print("\nRun complete.")

    from scripts.bench import reporter

    rows = reporter.load_sweep(db_path, sweep_id)
    reporter.print_summary_table(rows)
    reporter.print_ranking(rows)

    if args.output_json:
        reporter.export_json(rows, Path(args.output_json))
        print(f"JSON exported: {args.output_json}")
    if args.output_csv:
        reporter.export_csv(rows, Path(args.output_csv))
        print(f"CSV exported: {args.output_csv}")


# ── CLI ───────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local LLM benchmark runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="bench.toml",
        help="Path to bench.toml (default: bench.toml)",
    )
    parser.add_argument(
        "--tier",
        default=None,
        help="Filter by tier: speed|coding|prefill_shared|prefill_unshared|boundary",
    )
    parser.add_argument(
        "--resume",
        metavar="SWEEP_ID",
        default=None,
        help="Continue a previous sweep, skipping already-recorded cases",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("ID1", "ID2"),
        default=None,
        help="Print side-by-side comparison table for two sweep IDs",
    )
    parser.add_argument(
        "--generate-fixtures",
        action="store_true",
        help="Create scripts/bench/fixtures/project_summary_50k.txt",
    )
    parser.add_argument(
        "--browse",
        action="store_true",
        help="Open bench.db in datasette browser",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Override default bench.db path",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        metavar="PATH",
        help="Export sweep results to JSON",
    )
    parser.add_argument(
        "--output-csv",
        default=None,
        metavar="PATH",
        help="Export sweep results to CSV",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else _get_db_path()

    if args.generate_fixtures:
        _generate_fixtures()
        return 0

    if args.browse:
        _browse(db_path)
        return 0

    if args.compare:
        _compare(args.compare[0], args.compare[1], db_path)
        return 0

    _run(args, db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
