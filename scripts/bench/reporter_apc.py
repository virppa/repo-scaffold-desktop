"""APC (Automatic Prefix Caching) effectiveness.

Public API: compute_apc_speedup(), print_apc_section().
"""

from __future__ import annotations

from typing import Any

from scripts.bench._reporter_helpers import _fmt, _get_ttft, _median

_APC_W = 93


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


def print_apc_section(rows: list[dict[str, Any]]) -> None:
    """Print APC effectiveness table with conditional effectiveness labels."""
    entries = compute_apc_speedup(rows)
    if not entries:
        return

    print("=" * _APC_W)
    print("APC EFFECTIVENESS  (speedup = prefill_unshared_ttft / prefill_shared_ttft)")
    print("=" * _APC_W)
    header = (
        f"{'Model':<25}  {'Ctx':>6}  {'C':>3}  "
        f"{'Shared p50(s)':>13}  {'Unshared p50(s)':>15}  {'Speedup':>7}  {'Label':<14}"
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
        if ratio is None:
            label = ""
        elif ratio > 1.5:
            label = "APC effective"
        elif ratio < 1.1:
            label = "no APC benefit"
        else:
            label = ""
        line = (
            f"{model_str:<25}  {ctx_str:>6}  {c_str:>3}  "
            f"{shared_str:>13}  {unshared_str:>15}  {ratio_str:>7}  {label:<14}"
        )
        print(line)

    print("=" * _APC_W + "\n")
