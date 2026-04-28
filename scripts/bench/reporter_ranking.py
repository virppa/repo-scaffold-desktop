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
    _percentile,
)
from scripts.bench.reporter_apc import print_apc_section


def _quality_tier_rank(
    task_pct: float | None,
    high_pct: float,
    med_pct: float,
) -> int:
    """0 = HIGH (≥high_pct), 1 = MED (≥med_pct), 2 = speed-only (no coding data).

    Lower rank sorts first — quality beats speed within the composite ordering.
    Models with no coding data are not penalised by the eligibility gate but
    sort after all models with quality measurements.
    """
    if task_pct is None:
        return 2
    if task_pct >= high_pct:
        return 0
    return 1


def _quality_tier_label(task_pct: float | None, high_pct: float, med_pct: float) -> str:
    return ("HIGH", "MED", "—")[_quality_tier_rank(task_pct, high_pct, med_pct)]


def _is_eligible(
    rows_for_config: list[dict[str, Any]],
    *,
    min_useful_ctx: int = 4096,
    min_throughput_toks_per_s: float = 5.0,
    min_task_success_pct: float = 70.0,
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

    # task_success >= min_task_success_pct for coding rows that have quality data
    coding_with_quality = [
        r
        for r in real_runs
        if r.get("tier") == "coding" and r.get("quality_task_success") is not None
    ]
    if coding_with_quality:
        successes = sum(1 for r in coding_with_quality if r.get("quality_task_success"))
        rate = successes / len(coding_with_quality)
        if rate < min_task_success_pct / 100:
            return f"task success {rate:.0%} < {min_task_success_pct:.0f}%"

    return None


def compute_concurrency_efficiency(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, Any, Any], float | None]:
    """Compute aggregate throughput speedup per (backend, model, ctx, concurrency).

    speedup = (N * median(toks/s @ N)) / median(toks/s @ 1)

    A value of 1.68 means c=N delivers 68% more total tokens/s than c=1.
    Values >1 mean concurrency is beneficial; near 1 means no gain; <1 means overhead.
    Only repeat_index >= 1 rows are used. Guards against zero and None baselines.
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
        if baseline is None or baseline == 0.0:
            result[(backend, model, ctx, conc)] = None
            continue
        median_tok = _median(vals)
        if median_tok is None:
            result[(backend, model, ctx, conc)] = None
        else:
            result[(backend, model, ctx, conc)] = (float(conc) * median_tok) / baseline

    return result


_CONC_W = 90


def print_concurrency_scaling_section(rows: list[dict[str, Any]]) -> None:
    """Print Concurrency Scaling section grouped by (backend, model, ctx).

    Each group shows a table: C | Per-req tok/s | Agg tok/s | TTFT p50 | Speedup | Label
    Speedup = (c * tok_at_c) / tok_at_1 — values >1 mean concurrency helps.
    """
    # Collect per-request tok/s and TTFT keyed by (backend, model, ctx, conc)
    tok_map: dict[tuple[str, str, Any, Any], list[float]] = {}
    ttft_map: dict[tuple[str, str, Any, Any], list[float]] = {}
    for row in rows:
        if row.get("repeat_index", 0) < 1:
            continue
        key: tuple[str, str, Any, Any] = (
            str(row.get("backend_id") or ""),
            str(row.get("model_id") or ""),
            row.get("context_size"),
            row.get("concurrency"),
        )
        tok = row.get("throughput_tok_s")
        if tok is not None:
            tok_map.setdefault(key, []).append(float(tok))
        ttft = row.get("ttft_s")
        if ttft is not None:
            ttft_map.setdefault(key, []).append(float(ttft))

    # Group by (backend, model, ctx)
    groups: dict[tuple[str, str, Any], list[Any]] = {}
    all_concs: set[Any] = set()
    for backend, model, ctx, conc in tok_map:
        groups.setdefault((backend, model, ctx), []).append(conc)
        all_concs.add(conc)

    if not any(c != 1 for c in all_concs):
        return  # no concurrency data

    print("=" * _CONC_W)
    print("CONCURRENCY SCALING  (Speedup = c×tok/s_c / tok/s_1 — higher is better)")
    print("=" * _CONC_W)

    for (backend, model, ctx), concs in sorted(groups.items()):
        model_short = model.split("/")[-1][:40]
        print(f"\n  {backend} / {model_short} / ctx={ctx}")
        hdr = f"  {'C':>3}  {'Per-req tok/s':>13}  {'Agg tok/s':>9}"
        hdr += f"  {'TTFT p50':>8}  {'Speedup':>7}  Label"
        print(hdr)
        print(f"  {'-' * 3}  {'-' * 13}  {'-' * 9}  {'-' * 8}  {'-' * 7}  -----")

        baseline_tok: float | None = None
        baseline_key = (backend, model, ctx, 1)
        if baseline_key in tok_map:
            baseline_tok = _median(tok_map[baseline_key])

        for conc in sorted(concs):
            key = (backend, model, ctx, conc)
            per_req = _median(tok_map.get(key, []))
            ttft_p50 = _median(ttft_map.get(key, []))
            agg = (float(conc) * per_req) if per_req is not None else None
            if conc == 1 or baseline_tok is None or baseline_tok == 0 or agg is None:
                speedup_str = "1.00x" if conc == 1 else "N/A"
                label = "(baseline)" if conc == 1 else ""
            else:
                speedup = agg / baseline_tok
                speedup_str = f"{speedup:.2f}x"
                if speedup > 1.5:
                    label = "scales well"
                elif speedup > 1.1:
                    label = "partial gain"
                elif speedup > 0.9:
                    label = "no gain"
                else:
                    label = "overhead"
            per_req_str = _fmt(per_req, ".0f")
            agg_str = _fmt(agg, ".0f")
            ttft_str = _fmt(ttft_p50, ".2f") + "s" if ttft_p50 is not None else "--"
            row = (
                f"  {conc:>3}  {per_req_str:>13}  {agg_str:>9}"
                f"  {ttft_str:>8}  {speedup_str:>7}  {label}"
            )
            print(row)

    print("\n" + "=" * _CONC_W + "\n")


_W = 130


def print_ranking(
    rows: list[dict[str, Any]],
    *,
    min_useful_ctx: int = 4096,
    min_throughput_toks_per_s: float = 5.0,
    cv_threshold: float = 0.3,
    high_quality_pct: float = 85.0,
    medium_quality_pct: float = 70.0,
) -> None:
    """Print composite-scored ranking with recommendation banner.

    Sort order: quality tier first (HIGH ≥ high_quality_pct > MED ≥ medium_quality_pct >
    speed-only), then tok/s descending within each tier, then TTFT ascending.
    Models below medium_quality_pct are ineligible when coding data is present.
    Configs without coding data are eligible but sort after all quality-measured
    configs.
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
            min_task_success_pct=medium_quality_pct,
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
        concurrency: int = int(first.get("concurrency") or 1)
        conc_eff = efficiency_map.get(
            (
                str(first.get("backend_id") or ""),
                str(first.get("model_id") or ""),
                first.get("context_size"),
                concurrency,
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

        tok_p50 = _median(tok_values)
        agg_tok_p50 = (concurrency * tok_p50) if tok_p50 is not None else None
        eligible.append(
            {
                "config_key": config_key,
                "concurrency": concurrency,
                "ttft_p50": _median(ttft_values),
                "ttft_p95": _percentile(ttft_values, 95),
                "ttft_cv": ttft_cv,
                "unstable": unstable,
                "tok_p50": tok_p50,
                "agg_tok_p50": agg_tok_p50,
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

    # Composite sort: quality tier → aggregate tok/s descending → TTFT ascending
    eligible.sort(
        key=lambda e: (
            _quality_tier_rank(e["task_pct"], high_quality_pct, medium_quality_pct),
            -(e["agg_tok_p50"] if e["agg_tok_p50"] is not None else 0.0),
            e["ttft_p50"] if e["ttft_p50"] is not None else float("inf"),
        )
    )

    print("=" * _W)
    print(
        f"COMPOSITE RANKING  "
        f"(HIGH≥{high_quality_pct:.0f}% → MED≥{medium_quality_pct:.0f}%"
        f" → speed-only  |  within tier: agg.tok/s↓ then TTFT↑)"
    )
    print("=" * _W)
    header = (
        f"{'Rank':>4}  {'Config (backend/model/ctx/c)':<44}  "
        f"{'TTFT p50(s)':>10}  {'TTFT p95(s)':>10}  {'CV':>6}  "
        f"{'Tok/s p50':>9}  {'Agg.tok/s':>9}  {'Q.Tier':>6}  {'Stable':>6}  "
        f"{'VRAM Hdrm':>9}  {'Speedup':>7}"
    )
    print(header)
    print("-" * _W)

    for rank, entry in enumerate(eligible, start=1):
        ttft_p50_str = _fmt(entry["ttft_p50"], ".3f")
        ttft_p95_str = _fmt(entry["ttft_p95"], ".3f")
        cv_str = _fmt(entry["ttft_cv"], ".3f")
        tok_str = _fmt(entry["tok_p50"], ".0f")
        agg_str = _fmt(entry["agg_tok_p50"], ".0f")
        tier_str = _quality_tier_label(
            entry["task_pct"], high_quality_pct, medium_quality_pct
        )
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
        eff = entry["conc_eff"]
        eff_str = f"{eff:.2f}x" if eff is not None else "1.00x"
        row_str = (
            f"{rank:>4}  {entry['config_key']:<44}  "
            f"{ttft_p50_str:>10}  {ttft_p95_str:>10}  {cv_str:>6}  "
            f"{tok_str:>9}  {agg_str:>9}  {tier_str:>6}  {stable_str:>6}  "
            f"{headroom_str:>9}  {eff_str:>7}"
        )
        print(row_str)

    print("-" * _W)
    best = eligible[0]
    best_tier = _quality_tier_label(
        best["task_pct"], high_quality_pct, medium_quality_pct
    )
    print(f"\n  *** RECOMMENDED: {best['config_key']}  [Q.Tier: {best_tier}] ***")
    parts = []
    if best["ttft_p50"] is not None:
        parts.append(f"TTFT p50={best['ttft_p50']:.3f}s")
    if best["ttft_p95"] is not None:
        parts.append(f"TTFT p95={best['ttft_p95']:.3f}s")
    if best["ttft_cv"] is not None:
        parts.append(f"CV={best['ttft_cv']:.3f}")
    if best["tok_p50"] is not None:
        parts.append(f"tok/s p50={best['tok_p50']:.0f}")
    if best["agg_tok_p50"] is not None and best["concurrency"] > 1:
        parts.append(f"agg.tok/s={best['agg_tok_p50']:.0f}")
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
