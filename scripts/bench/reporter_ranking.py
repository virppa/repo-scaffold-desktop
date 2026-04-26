"""Quality-gated ranking: _is_eligible(), compute_concurrency_efficiency(),
print_ranking(), and print_concurrency_scaling_section()."""

from __future__ import annotations

from typing import Any

from scripts.bench._reporter_helpers import (
    OOM_RISK_HEADROOM_GB,
    VRAM_HEADROOM_WARN_GB,
    _cv,
    _fmt,
    _median,
    _pct,
    _percentile,
)
from scripts.bench.reporter_apc import print_apc_section


def _is_eligible(
    rows_for_config: list[dict[str, Any]],
    *,
    min_useful_ctx: int = 4096,
    min_throughput_toks_per_s: float = 5.0,
) -> str | None:
    """Return None if the config passes all quality gates, or a rejection reason string.

    Evaluated per (model, context_size, concurrency) group. Each failing gate returns
    a short human-readable reason for display next to the disqualified config.
    """
    if not rows_for_config:
        return "no data"

    real_runs = [r for r in rows_for_config if r.get("repeat_index", 0) >= 1]
    if not real_runs:
        return "no real runs"

    # OOM at this specific config
    if any(r.get("outcome") == "oom" for r in real_runs):
        return "OOM"

    # CPU offload anywhere in this config
    if any(r.get("cpu_offload_detected") for r in real_runs):
        return "CPU offload"

    # VRAM headroom too low — risk of OOM during extended use
    peak_vram_values = [
        float(r["peak_vram_gb"]) for r in real_runs if r.get("peak_vram_gb") is not None
    ]
    total_vram: float | None = next(
        (
            float(r["total_vram_gb"])
            for r in real_runs
            if r.get("total_vram_gb") is not None
        ),
        None,
    )
    if peak_vram_values and total_vram is not None:
        median_peak = _median(peak_vram_values)
        if median_peak is not None:
            headroom = total_vram - median_peak
            if headroom < OOM_RISK_HEADROOM_GB:
                return (
                    f"OOM risk (VRAM headroom {headroom:.2f} GB"
                    f" < {OOM_RISK_HEADROOM_GB} GB)"
                )

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


_CONC_W = 80


def print_concurrency_scaling_section(rows: list[dict[str, Any]]) -> None:
    """Print Concurrency Scaling section with per-config efficiency and labels.

    Labels: efficiency < 0.5 → 'serialised', efficiency > 0.8 → 'scales well'.
    Only configs with a computed (non-None) efficiency are shown.
    """
    efficiency_map = compute_concurrency_efficiency(rows)
    entries = {k: v for k, v in efficiency_map.items() if v is not None}
    if not entries:
        return

    print("=" * _CONC_W)
    print("CONCURRENCY SCALING")
    print("=" * _CONC_W)
    header = (
        f"{'Backend/Model':<35}  {'Ctx':>6}  {'C':>3}  {'Conc.Eff':>9}  {'Label':<14}"
    )
    print(header)
    print("-" * _CONC_W)

    for (backend, model, ctx, conc), eff in sorted(entries.items()):
        config_str = f"{backend}/{model}"[:35]
        ctx_str = _fmt(ctx)
        c_str = _fmt(conc)
        eff_str = f"{eff:.3f}"
        if eff < 0.5:
            label = "serialised"
        elif eff > 0.8:
            label = "scales well"
        else:
            label = ""
        print(f"{config_str:<35}  {ctx_str:>6}  {c_str:>3}  {eff_str:>9}  {label:<14}")

    print("=" * _CONC_W + "\n")


_W = 130


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
        print_concurrency_scaling_section(rows)
        return

    # Sort by TTFT p50 ascending (lower is better); fall back to tok/s descending
    eligible.sort(
        key=lambda e: (
            e["ttft_p50"] if e["ttft_p50"] is not None else float("inf"),
            -(e["tok_p50"] if e["tok_p50"] is not None else 0.0),
        )
    )

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
    print_concurrency_scaling_section(rows)
