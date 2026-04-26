"""Sweep comparison: _metric_delta() and print_compare_table()."""

from __future__ import annotations

from typing import Any

from scripts.bench._reporter_helpers import _fmt

_COMPARE_W = 160


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
