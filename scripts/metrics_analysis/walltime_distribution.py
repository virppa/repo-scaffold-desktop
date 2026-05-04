"""Distribution of worker wall times in app.db.

Used to calibrate watcher-side thresholds (e.g. WOR-381's wall-time
timeout). Future Streamlit dashboards can vendor the same SQL.

Usage:

    python scripts/metrics_analysis/walltime_distribution.py
    python scripts/metrics_analysis/walltime_distribution.py --since-days 30
    python scripts/metrics_analysis/walltime_distribution.py --mode local
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

# Allow running this script directly without `python -m` — add repo root to path.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.metrics import MetricsStore  # noqa: E402


def _percentile(values: list[float], p: float) -> float:
    """Return the value at percentile p (0.0–1.0) of a sorted-once list."""
    if not values:
        return float("nan")
    n = len(values)
    return values[min(n - 1, int(n * p))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Only include sessions recorded in the last N days.",
    )
    ap.add_argument(
        "--mode",
        choices=("local", "cloud"),
        default=None,
        help="Filter by implementation_mode.",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=10,
        help="How many longest sessions to show in the tail (default 10).",
    )
    args = ap.parse_args()

    db = MetricsStore.get_db_path()
    print(f"DB: {db}\n")

    # Static SQL with named placeholders. NULL placeholders bypass the
    # respective filter via `IS NULL OR` so we don't need to compose the
    # WHERE clause dynamically.
    sql = """
        SELECT ticket_id, local_wall_time, outcome, implementation_mode, recorded_at
        FROM ticket_metrics
        WHERE local_wall_time IS NOT NULL
          AND local_wall_time > 0
          AND (:days IS NULL OR recorded_at >= datetime('now', :days || ' days'))
          AND (:mode IS NULL OR implementation_mode = :mode)
        ORDER BY local_wall_time DESC
    """
    days_param = f"-{int(args.since_days)}" if args.since_days is not None else None
    bind = {"days": days_param, "mode": args.mode}

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(sql, bind).fetchall()
    times = [r["local_wall_time"] for r in rows]

    print(f"Sessions: {len(times)}")
    if args.since_days is not None:
        print(f"Window: last {args.since_days} days")
    if args.mode is not None:
        print(f"Mode filter: {args.mode}")
    print()

    if not times:
        print("(no data)")
        return 0

    sorted_times = sorted(times)
    median = statistics.median(times)
    mean = statistics.mean(times)
    p25 = _percentile(sorted_times, 0.25)
    p75 = _percentile(sorted_times, 0.75)
    p90 = _percentile(sorted_times, 0.90)
    p95 = _percentile(sorted_times, 0.95)
    p99 = _percentile(sorted_times, 0.99)

    print("=== Wall-time distribution ===")
    print(f"  min   : {sorted_times[0]:>8.1f}s = {sorted_times[0] / 60:>5.1f} min")
    print(f"  p25   : {p25:>8.1f}s = {p25 / 60:>5.1f} min")
    print(f"  median: {median:>8.1f}s = {median / 60:>5.1f} min")
    print(f"  p75   : {p75:>8.1f}s = {p75 / 60:>5.1f} min")
    print(f"  p90   : {p90:>8.1f}s = {p90 / 60:>5.1f} min")
    print(f"  p95   : {p95:>8.1f}s = {p95 / 60:>5.1f} min")
    print(f"  p99   : {p99:>8.1f}s = {p99 / 60:>5.1f} min")
    print(f"  max   : {sorted_times[-1]:>8.1f}s = {sorted_times[-1] / 60:>5.1f} min")
    print(f"  mean  : {mean:>8.1f}s = {mean / 60:>5.1f} min")
    print()

    print(f"=== Top {args.top} longest sessions ===")
    header = (
        f"{'ticket':<10} {'sec':>8} {'min':>6}  "
        f"{'outcome':<10} {'mode':<8}  recorded_at"
    )
    print(header)
    for r in rows[: args.top]:
        print(
            f"{r['ticket_id']:<10} {r['local_wall_time']:>8.0f} "
            f"{r['local_wall_time'] / 60:>6.1f}  {r['outcome'] or '?':<10} "
            f"{r['implementation_mode'] or '?':<8}  {r['recorded_at']}"
        )
    print()

    print("=== Counts over thresholds ===")
    for thr_min in (10, 15, 20, 30, 45, 60, 90, 120):
        over = sum(1 for t in times if t > thr_min * 60)
        pct = 100 * over / len(times)
        print(f"  > {thr_min:>3} min: {over:>4} sessions ({pct:>5.1f}%)")
    print()

    long_rows = [r for r in rows if r["local_wall_time"] > 30 * 60]
    if long_rows:
        print("=== Sessions > 30 minutes — outcome breakdown ===")
        oc = Counter(r["outcome"] for r in long_rows)
        for outcome, n in oc.most_common():
            print(f"  {outcome or '(none)':<12} {n}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
