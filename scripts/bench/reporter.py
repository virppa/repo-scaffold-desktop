"""Benchmark reporter: console tables, quality-gated ranking, and data export.

Public API is re-exported from sub-modules so all existing callers remain unchanged:
  reporter_ranking  — _is_eligible, compute_concurrency_efficiency, print_ranking,
                      print_concurrency_scaling_section
  reporter_apc      — compute_apc_speedup, print_apc_section
  reporter_compare  — _metric_delta, print_compare_table
  _reporter_helpers — shared constants and formatting utilities
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from scripts.bench._reporter_helpers import (
    OOM_RISK_HEADROOM_GB,
    VRAM_HEADROOM_WARN_GB,
    _bool_col,
    _cv,
    _fmt,
    _get_ttft,
    _median,
    _pct,
    _percentile,
)
from scripts.bench.reporter_apc import compute_apc_speedup, print_apc_section
from scripts.bench.reporter_compare import _metric_delta, print_compare_table
from scripts.bench.reporter_ranking import (
    _is_eligible,
    compute_concurrency_efficiency,
    print_concurrency_scaling_section,
    print_ranking,
)

__all__ = [
    # constants
    "VRAM_HEADROOM_WARN_GB",
    "OOM_RISK_HEADROOM_GB",
    # helpers (used by tests)
    "_fmt",
    "_bool_col",
    "_pct",
    "_median",
    "_percentile",
    "_cv",
    "_get_ttft",
    # ranking
    "_is_eligible",
    "compute_concurrency_efficiency",
    "print_ranking",
    "print_concurrency_scaling_section",
    # APC
    "compute_apc_speedup",
    "print_apc_section",
    # compare
    "_metric_delta",
    "print_compare_table",
    # this module
    "load_sweep",
    "print_summary_table",
    "export_json",
    "export_csv",
]


# ── DB loader ─────────────────────────────────────────────────────────────────


def load_sweep(db_path: Path, sweep_id: str) -> list[dict[str, Any]]:
    """Load all rows recorded under *sweep_id* from bench.db."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM bench_run WHERE run_id LIKE ?",
            (f"{sweep_id}::%",),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── Summary table ─────────────────────────────────────────────────────────────

_SUMMARY_COLS = [
    ("Model", 20),
    ("Tier", 14),
    ("Ctx", 6),
    ("C", 3),
    ("R", 3),
    ("TTFT(s)", 8),
    ("Wall(s)", 8),
    ("Tok/s", 7),
    ("Agg.tok/s", 9),
    ("VRAM(GB)", 9),
    ("GPU%", 5),
    ("OOM", 5),
    ("Offload", 7),
    ("Outcome", 7),
]


def print_summary_table(rows: list[dict[str, Any]]) -> None:
    """Print a per-case summary table to stdout."""
    if not rows:
        print("\n(no results for this sweep)")
        return

    show_ttfut = any(r.get("ttfut_s") is not None for r in rows)
    show_throttle = any(r.get("thermal_throttle_detected") is True for r in rows)
    cols = list(_SUMMARY_COLS)
    if show_ttfut:
        ttft_idx = next(i for i, (name, _) in enumerate(cols) if name == "TTFT(s)")
        cols.insert(ttft_idx + 1, ("TTFUT(s)", 8))
    if show_throttle:
        cols.append(("Throttle", 8))

    header = "  ".join(name.ljust(w) for name, w in cols)
    sep = "  ".join("-" * w for _, w in cols)
    print(f"\n{'=' * len(sep)}")
    print("SWEEP SUMMARY")
    print(f"{'=' * len(sep)}")
    print(header)
    print(sep)

    for r in rows:
        oom = r.get("outcome") == "oom"
        offload = bool(r.get("cpu_offload_detected"))
        vals = [
            str(r.get("model_id") or "--")[:20],
            str(r.get("tier") or "--")[:14],
            _fmt(r.get("context_size")),
            _fmt(r.get("concurrency")),
            _fmt(r.get("repeat_index")),
            _fmt(r.get("ttft_s"), ".2f"),
        ]
        if show_ttfut:
            vals.append(_fmt(r.get("ttfut_s"), ".2f"))
        conc = r.get("concurrency") or 1
        tok_s = r.get("throughput_tok_s")
        agg_tok_s = (conc * tok_s) if tok_s is not None else None
        vals += [
            _fmt(r.get("wall_time_s"), ".2f"),
            _fmt(tok_s, ".0f"),
            _fmt(agg_tok_s, ".0f"),
            _fmt(r.get("peak_vram_gb"), ".1f"),
            _pct(r.get("avg_gpu_util_pct")),
            "Yes" if oom else "No",
            "Yes" if offload else "No",
            str(r.get("outcome") or "--")[:7],
        ]
        if show_throttle:
            vals.append(_bool_col(r.get("thermal_throttle_detected")))
        line = "  ".join(v.ljust(w) for v, (_, w) in zip(vals, cols))
        print(line)

    print(f"{'=' * len(sep)}\n")


# ── Export ────────────────────────────────────────────────────────────────────


def export_json(rows: list[dict[str, Any]], path: Path) -> None:
    """Export sweep rows to JSON (list of objects)."""
    path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")


def export_csv(rows: list[dict[str, Any]], path: Path) -> None:
    """Export sweep rows to CSV."""
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
