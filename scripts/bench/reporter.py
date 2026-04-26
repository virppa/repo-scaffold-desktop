"""Benchmark reporter: console tables, quality-gated ranking, and data export."""

from __future__ import annotations

import csv
import json
import sqlite3
import statistics
from pathlib import Path
from typing import Any

VRAM_HEADROOM_WARN_GB: float = 2.0

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


# ── Formatting helpers ────────────────────────────────────────────────────────


def _fmt(value: Any, fmt: str = "", na: str = "--") -> str:
    if value is None:
        return na
    try:
        return format(value, fmt)
    except (TypeError, ValueError):
        return str(value)


def _bool_col(value: Any) -> str:
    if value is None:
        return "--"
    return "Yes" if value else "No"


def _pct(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.0f}%"


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return statistics.median(clean)


def _percentile(values: list[float], pct: int) -> float | None:
    """Return pct-th percentile via linear interpolation; None if < 2 values."""
    if len(values) < 2:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    idx = pct / 100 * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def _cv(values: list[float]) -> float | None:
    """Return coefficient of variation (stddev/mean); None if < 2 values or mean==0."""
    if len(values) < 2:
        return None
    m = statistics.mean(values)
    if m == 0.0:
        return None
    return statistics.stdev(values) / m


def _get_ttft(row: dict[str, Any]) -> float | None:
    """Prefer prompt_eval_duration_s; fall back to ttft_s."""
    for key in ("prompt_eval_duration_s", "ttft_s"):
        v = row.get(key)
        if v is not None:
            return float(v)
    return None


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
        vals += [
            _fmt(r.get("wall_time_s"), ".2f"),
            _fmt(r.get("throughput_tok_s"), ".0f"),
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


# ── Concurrency efficiency ────────────────────────────────────────────────────


def compute_concurrency_efficiency(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, Any, Any], float | None]:
    """Compute concurrency efficiency per (backend_id, model_id, ctx, concurrency).

    efficiency = median(toks/s @ N) / (N * median(toks/s @ 1))

    A value near 1.0 means near-linear scaling; >1.0 is super-linear (valid).
    Only repeat_index >= 1 rows are used. Returns an empty dict when no
    concurrency>1 data is present. Guards against zero and None baselines.
    """
    tok_map: dict[tuple[str, str, Any, Any], list[float]] = {}
    for row in rows:
        if row.get("repeat_index", 0) < 1:
            continue
        tok = row.get("throughput_tok_s")
        if tok is None:
            continue
        key: tuple[str, str, Any, Any] = (
            str(row.get("backend_id") or ""),
            str(row.get("model_id") or ""),
            row.get("context_size"),
            row.get("concurrency"),
        )
        tok_map.setdefault(key, []).append(float(tok))

    baselines: dict[tuple[str, str, Any], float | None] = {}
    for (backend, model, ctx, conc), vals in tok_map.items():
        if conc == 1:
            baselines[(backend, model, ctx)] = _median(vals)

    result: dict[tuple[str, str, Any, Any], float | None] = {}
    for (backend, model, ctx, conc), vals in tok_map.items():
        if conc is None or conc <= 1:
            continue
        baseline = baselines.get((backend, model, ctx))
        if baseline is None:
            result[(backend, model, ctx, conc)] = None
            continue
        if baseline == 0.0:
            result[(backend, model, ctx, conc)] = None
            continue
        median_tok = _median(vals)
        if median_tok is None:
            result[(backend, model, ctx, conc)] = None
        else:
            result[(backend, model, ctx, conc)] = median_tok / (float(conc) * baseline)

    return result


# ── Quality-gated ranking ─────────────────────────────────────────────────────


def _is_eligible(
    rows_for_config: list[dict[str, Any]],
    *,
    min_useful_ctx: int = 4096,
    min_throughput_toks_per_s: float = 5.0,
) -> str | None:
    """Return None if the config passes all quality gates, or a rejection reason string.

    Evaluated per (model, context_size, concurrency) group — not across all configs for
    a model. Each gate that fails returns a short human-readable reason so callers can
    display it next to the disqualified config.
    """
    if not rows_for_config:
        return "no data"

    real_runs = [r for r in rows_for_config if r.get("repeat_index", 0) >= 1]
    if not real_runs:
        return "no real runs"

    # OOM at this specific (model, ctx, concurrency) config
    if any(r.get("outcome") == "oom" for r in real_runs):
        return "OOM"

    # CPU offload anywhere in this config
    if any(r.get("cpu_offload_detected") for r in real_runs):
        return "CPU offload"

    # error_rate <= 5%
    errors = sum(1 for r in real_runs if r.get("outcome") not in ("ok", None))
    if errors / len(real_runs) > 0.05:
        return f"error rate {errors / len(real_runs):.0%}"

    # Context size too small: all runs used a context below the minimum useful threshold
    ctx_values = [
        r["context_size"] for r in real_runs if r.get("context_size") is not None
    ]
    if ctx_values and all(c < min_useful_ctx for c in ctx_values):
        return f"context too small (max {max(ctx_values)} < {min_useful_ctx})"

    # Throughput too low: median tok/s falls below the configured floor
    tok_values = [
        r["throughput_tok_s"]
        for r in real_runs
        if r.get("throughput_tok_s") is not None
    ]
    median_tok = _median(tok_values)
    if median_tok is not None and median_tok < min_throughput_toks_per_s:
        floor = min_throughput_toks_per_s
        return f"throughput too low ({median_tok:.1f} tok/s < {floor} tok/s)"

    # task_success >= 70% for coding rows that have quality data
    coding_with_quality = [
        r
        for r in real_runs
        if r.get("tier") == "coding" and r.get("quality_task_success") is not None
    ]
    if coding_with_quality:
        successes = sum(1 for r in coding_with_quality if r.get("quality_task_success"))
        if successes / len(coding_with_quality) < 0.70:
            return f"task success {successes / len(coding_with_quality):.0%} < 70%"

    return None


def print_ranking(
    rows: list[dict[str, Any]],
    *,
    min_useful_ctx: int = 4096,
    min_throughput_toks_per_s: float = 5.0,
    cv_threshold: float = 0.3,
) -> None:
    """Print quality-gated ranking with recommendation banner.

    Ineligible configs are listed below the ranking table with their rejection reason.
    Threshold parameters keep defaults so existing callers need no changes.
    When a config has > 1 repeat, ttft_p95 and ttft_cv are computed and the config is
    flagged unstable when ttft_cv exceeds cv_threshold (default 0.3).
    """
    if not rows:
        return

    efficiency_map = compute_concurrency_efficiency(rows)

    # Group by (backend_id, model_id, context_size, concurrency) — one row per config
    by_config: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = (
            f"{r.get('backend_id', '')}/{r.get('model_id', '')} "
            f"ctx={r.get('context_size')} c={r.get('concurrency')}"
        )
        by_config.setdefault(key, []).append(r)

    eligible: list[dict[str, Any]] = []
    ineligible: list[tuple[str, str]] = []  # (config_key, rejection_reason)
    for config_key, config_rows in by_config.items():
        reason = _is_eligible(
            config_rows,
            min_useful_ctx=min_useful_ctx,
            min_throughput_toks_per_s=min_throughput_toks_per_s,
        )
        if reason is not None:
            ineligible.append((config_key, reason))
            continue
        real_runs = [r for r in config_rows if r.get("repeat_index", 0) >= 1]
        ttft_values: list[float] = [
            r["ttft_s"] for r in real_runs if r.get("ttft_s") is not None
        ]
        tok_values: list[float] = [
            r["throughput_tok_s"]
            for r in real_runs
            if r.get("throughput_tok_s") is not None
        ]
        coding_with_q = [
            r
            for r in real_runs
            if r.get("tier") == "coding" and r.get("quality_task_success") is not None
        ]
        task_pct: float | None = None
        if coding_with_q:
            successes = sum(1 for r in coding_with_q if r.get("quality_task_success"))
            task_pct = 100.0 * successes / len(coding_with_q)

        ttft_cv = _cv(ttft_values)
        unstable: bool | None = None
        if ttft_cv is not None:
            unstable = ttft_cv > cv_threshold

        first = real_runs[0] if real_runs else config_rows[0]
        conc_eff = efficiency_map.get(
            (
                str(first.get("backend_id") or ""),
                str(first.get("model_id") or ""),
                first.get("context_size"),
                first.get("concurrency"),
            )
        )

        peak_vram_values: list[float] = [
            float(r["peak_vram_gb"])
            for r in real_runs
            if r.get("peak_vram_gb") is not None
        ]
        peak_vram_p50 = _median(peak_vram_values)
        total_vram: float | None = next(
            (
                float(r["total_vram_gb"])
                for r in real_runs
                if r.get("total_vram_gb") is not None
            ),
            None,
        )
        vram_headroom_gb: float | None = None
        if peak_vram_p50 is not None and total_vram is not None:
            vram_headroom_gb = total_vram - peak_vram_p50

        eligible.append(
            {
                "config_key": config_key,
                "ttft_p50": _median(ttft_values),
                "ttft_p95": _percentile(ttft_values, 95),
                "ttft_cv": ttft_cv,
                "unstable": unstable,
                "tok_p50": _median(tok_values),
                "task_pct": task_pct,
                "conc_eff": conc_eff,
                "vram_headroom_gb": vram_headroom_gb,
            }
        )

    if not eligible:
        print("No quality-eligible configurations found for this sweep.\n")
        if ineligible:
            print("INELIGIBLE CONFIGS:")
            for config_key, reason in ineligible:
                print(f"  {config_key}  [{reason}]")
            print()
        print_apc_section(rows)
        return

    # Sort by TTFT p50 ascending (lower is better); fall back to tok/s descending
    eligible.sort(
        key=lambda e: (
            e["ttft_p50"] if e["ttft_p50"] is not None else float("inf"),
            -(e["tok_p50"] if e["tok_p50"] is not None else 0.0),
        )
    )

    _W = 130
    print("=" * _W)
    print("QUALITY-ELIGIBLE RANKING  (oom=No, offload=No, err≤5%, task≥70%)")
    print("=" * _W)
    header = (
        f"{'Rank':>4}  {'Config (backend/model/ctx/c)':<44}  "
        f"{'TTFT p50(s)':>10}  {'TTFT p95(s)':>10}  {'CV':>6}  "
        f"{'Tok/s p50':>9}  {'Task%':>5}  {'Stable':>6}  "
        f"{'VRAM Hdrm':>9}  {'Conc.Eff':>9}"
    )
    print(header)
    print("-" * _W)

    for rank, entry in enumerate(eligible, start=1):
        ttft_p50_str = _fmt(entry["ttft_p50"], ".3f")
        ttft_p95_str = _fmt(entry["ttft_p95"], ".3f")
        cv_str = _fmt(entry["ttft_cv"], ".3f")
        tok_str = _fmt(entry["tok_p50"], ".0f")
        task_str = _pct(entry["task_pct"])
        if entry["unstable"] is None:
            stable_str = "--"
        elif entry["unstable"]:
            stable_str = "[!]"
        else:
            stable_str = "OK"
        headroom = entry["vram_headroom_gb"]
        if headroom is None:
            headroom_str = "N/A"
        elif headroom < VRAM_HEADROOM_WARN_GB:
            headroom_str = f"{headroom:.1f}[!]"
        else:
            headroom_str = f"{headroom:.1f}"
        eff_str = f"{entry['conc_eff']:.3f}" if entry["conc_eff"] is not None else "N/A"
        row_str = (
            f"{rank:>4}  {entry['config_key']:<44}  "
            f"{ttft_p50_str:>10}  {ttft_p95_str:>10}  {cv_str:>6}  "
            f"{tok_str:>9}  {task_str:>5}  {stable_str:>6}  "
            f"{headroom_str:>9}  {eff_str:>9}"
        )
        print(row_str)

    print("-" * _W)
    best = eligible[0]
    print(f"\n  *** RECOMMENDED: {best['config_key']} ***")
    parts = []
    if best["ttft_p50"] is not None:
        parts.append(f"TTFT p50={best['ttft_p50']:.3f}s")
    if best["ttft_p95"] is not None:
        parts.append(f"TTFT p95={best['ttft_p95']:.3f}s")
    if best["ttft_cv"] is not None:
        parts.append(f"CV={best['ttft_cv']:.3f}")
    if best["tok_p50"] is not None:
        parts.append(f"tok/s p50={best['tok_p50']:.0f}")
    if best["task_pct"] is not None:
        parts.append(f"task={best['task_pct']:.0f}%")
    if parts:
        print("  " + " | ".join(parts))
    print("=" * _W + "\n")

    if ineligible:
        print("INELIGIBLE CONFIGS:")
        for config_key, reason in ineligible:
            print(f"  {config_key}  [{reason}]")
        print()

    print_apc_section(rows)


# ── APC effectiveness ─────────────────────────────────────────────────────────


def compute_apc_speedup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute KV-cache speedup ratio per (model_id, context_size, concurrency).

    Only prefill_shared / prefill_unshared tier rows (repeat_index >= 1) are used.
    Returns a list of dicts; apc_speedup_ratio is None when either tier is absent
    or shared TTFT is zero (division-by-zero guard).
    """
    shared_ttfts: dict[tuple[str, Any, Any], list[float]] = {}
    unshared_ttfts: dict[tuple[str, Any, Any], list[float]] = {}

    for row in rows:
        tier = row.get("tier")
        if tier not in ("prefill_shared", "prefill_unshared"):
            continue
        if row.get("repeat_index", 0) < 1:
            continue
        ttft = _get_ttft(row)
        if ttft is None:
            continue
        key: tuple[str, Any, Any] = (
            str(row.get("model_id") or ""),
            row.get("context_size"),
            row.get("concurrency"),
        )
        target = shared_ttfts if tier == "prefill_shared" else unshared_ttfts
        target.setdefault(key, []).append(ttft)

    all_keys = sorted(
        set(shared_ttfts) | set(unshared_ttfts),
        key=lambda k: (k[0], k[1] or 0, k[2] or 0),
    )
    results: list[dict[str, Any]] = []
    for key in all_keys:
        model, context_size, concurrency = key
        shared_p50 = _median(shared_ttfts.get(key, []))
        unshared_p50 = _median(unshared_ttfts.get(key, []))
        ratio: float | None = None
        if shared_p50 is not None and shared_p50 > 0 and unshared_p50 is not None:
            ratio = unshared_p50 / shared_p50
        results.append(
            {
                "model": model,
                "context_size": context_size,
                "concurrency": concurrency,
                "shared_ttft_p50": shared_p50,
                "unshared_ttft_p50": unshared_p50,
                "apc_speedup_ratio": ratio,
            }
        )
    return results


_APC_W = 79


def print_apc_section(rows: list[dict[str, Any]]) -> None:
    """Print APC effectiveness table if prefill-tier rows are present."""
    entries = compute_apc_speedup(rows)
    if not entries:
        return

    print("=" * _APC_W)
    print("APC EFFECTIVENESS  (speedup = prefill_unshared_ttft / prefill_shared_ttft)")
    print("=" * _APC_W)
    header = (
        f"{'Model':<25}  {'Ctx':>6}  {'C':>3}  "
        f"{'Shared p50(s)':>13}  {'Unshared p50(s)':>15}  {'Speedup':>7}"
    )
    print(header)
    print("-" * _APC_W)

    for entry in entries:
        model_str = str(entry["model"] or "--")[:25]
        ctx_str = _fmt(entry["context_size"])
        c_str = _fmt(entry["concurrency"])
        shared_str = _fmt(entry["shared_ttft_p50"], ".3f")
        unshared_str = _fmt(entry["unshared_ttft_p50"], ".3f")
        ratio = entry["apc_speedup_ratio"]
        ratio_str = f"{ratio:.2f}x" if ratio is not None else "N/A"
        line = (
            f"{model_str:<25}  {ctx_str:>6}  {c_str:>3}  "
            f"{shared_str:>13}  {unshared_str:>15}  {ratio_str:>7}"
        )
        print(line)

    print("=" * _APC_W + "\n")


# ── Compare table ─────────────────────────────────────────────────────────────


def _fingerprint(run_id: str, sweep_id: str) -> str:
    """Extract case fingerprint from run_id by stripping the sweep prefix."""
    prefix = f"{sweep_id}::"
    if run_id.startswith(prefix):
        return run_id[len(prefix) :]
    return run_id


def _metric_delta(
    v1: float | None,
    v2: float | None,
    *,
    abs_fmt: str,
    higher_is_better: bool,
    regression_threshold_pct: float,
) -> tuple[str, bool]:
    """Return (delta_string, is_regression) for a metric pair.

    delta_string shows absolute change and % change (e.g. '+0.050(+15%)').
    is_regression is True when the metric degrades beyond regression_threshold_pct.
    None inputs always return ('--', False).
    """
    if v1 is None or v2 is None:
        return "--", False
    abs_d = v2 - v1
    if v1 == 0.0:
        return f"{abs_d:{abs_fmt}}(--)", False
    pct_d = (v2 - v1) / abs(v1) * 100.0
    delta_str = f"{abs_d:{abs_fmt}}({pct_d:+.0f}%)"
    if higher_is_better:
        is_regression = pct_d < -regression_threshold_pct
    else:
        is_regression = pct_d > regression_threshold_pct
    return delta_str, is_regression


_COMPARE_W = 160


def print_compare_table(
    rows1: list[dict[str, Any]],
    rows2: list[dict[str, Any]],
    id1: str,
    id2: str,
    *,
    regression_threshold_pct: float = 10.0,
) -> bool:
    """Print side-by-side comparison with delta columns and regression detection.

    Adds ΔTTFT and ΔTok/s columns showing absolute and % change.
    Rows where a metric degrades beyond regression_threshold_pct are prefixed with '!!'.
    Returns True if any regression was detected, False otherwise.
    """
    by_fp1 = {_fingerprint(r["run_id"], id1): r for r in rows1}
    by_fp2 = {_fingerprint(r["run_id"], id2): r for r in rows2}
    all_fps = sorted(set(by_fp1) | set(by_fp2))

    if not all_fps:
        print("No rows found for either sweep ID.")
        return False

    id1_short = id1[:20]
    id2_short = id2[:20]
    sep = "=" * _COMPARE_W
    print(f"\n{sep}")
    print(
        f"COMPARISON: {id1_short}  vs  {id2_short}"
        f"  (regression threshold: {regression_threshold_pct:.0f}%)"
    )
    print(sep)

    hdr = (
        f"   {'Model':<20}  {'Tier':<12}  {'Ctx':>6}  {'C':>3}  {'R':>3}  "
        f"{'TTFT-1(s)':>9}  {'TTFT-2(s)':>9}  {'ΔTTFT':>17}  "
        f"{'Tok/s-1':>8}  {'Tok/s-2':>8}  {'ΔTok/s':>14}  "
        f"{'OOM1':>5}  {'OOM2':>5}  {'Off1':>5}  {'Off2':>5}"
    )
    print(hdr)
    print("-" * _COMPARE_W)

    any_regression = False

    for fp in all_fps:
        r1 = by_fp1.get(fp)
        r2 = by_fp2.get(fp)

        if r1 is not None:
            model = str(r1.get("model_id") or "--")[:20]
            tier = str(r1.get("tier") or "--")[:12]
            ctx = _fmt(r1.get("context_size"))
            concurrency = _fmt(r1.get("concurrency"))
            repeat = _fmt(r1.get("repeat_index"))
        elif r2 is not None:
            model = str(r2.get("model_id") or "--")[:20]
            tier = str(r2.get("tier") or "--")[:12]
            ctx = _fmt(r2.get("context_size"))
            concurrency = _fmt(r2.get("concurrency"))
            repeat = _fmt(r2.get("repeat_index"))
        else:
            continue

        ttft1: float | None = r1.get("ttft_s") if r1 else None
        ttft2: float | None = r2.get("ttft_s") if r2 else None
        tok1: float | None = r1.get("throughput_tok_s") if r1 else None
        tok2: float | None = r2.get("throughput_tok_s") if r2 else None
        oom1 = "Yes" if (r1 and r1.get("outcome") == "oom") else "No"
        oom2 = "Yes" if (r2 and r2.get("outcome") == "oom") else "No"
        off1 = "Yes" if (r1 and r1.get("cpu_offload_detected")) else "No"
        off2 = "Yes" if (r2 and r2.get("cpu_offload_detected")) else "No"

        ttft_delta_str, ttft_reg = _metric_delta(
            ttft1,
            ttft2,
            abs_fmt="+.3f",
            higher_is_better=False,
            regression_threshold_pct=regression_threshold_pct,
        )
        tok_delta_str, tok_reg = _metric_delta(
            tok1,
            tok2,
            abs_fmt="+.0f",
            higher_is_better=True,
            regression_threshold_pct=regression_threshold_pct,
        )

        is_regression = ttft_reg or tok_reg
        if is_regression:
            any_regression = True
        prefix = "!!" if is_regression else "  "

        ttft_cols = (
            f"{_fmt(ttft1, '.3f'):>9}  {_fmt(ttft2, '.3f'):>9}  {ttft_delta_str:>17}"
        )
        tok_cols = (
            f"{_fmt(tok1, '.0f'):>8}  {_fmt(tok2, '.0f'):>8}  {tok_delta_str:>14}"
        )
        ann_cols = f"{oom1:>5}  {oom2:>5}  {off1:>5}  {off2:>5}"
        id_cols = (
            f"{prefix} {model:<20}  {tier:<12}  {ctx:>6}  {concurrency:>3}  {repeat:>3}"
        )
        print(f"{id_cols}  {ttft_cols}  {tok_cols}  {ann_cols}")

    print(sep + "\n")

    if any_regression:
        thr = f"{regression_threshold_pct:.0f}%"
        print(
            f"REGRESSIONS DETECTED (threshold: {thr})"
            f"  [ttft_s increase >{thr} | tok/s decrease >{thr}]\n"
        )

    return any_regression


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
