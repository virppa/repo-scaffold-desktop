"""Shared formatting helpers and constants used across reporter sub-modules."""

from __future__ import annotations

import statistics
from typing import Any

VRAM_HEADROOM_WARN_GB: float = 2.0
OOM_RISK_HEADROOM_GB: float = 0.5


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
