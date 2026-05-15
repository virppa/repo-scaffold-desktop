"""WOR-336 spike — automatable throughput forensic (no GPU required).

Reads the production metrics DB (``app.db``) and answers the WOR-336
hypotheses that do NOT need a fresh GPU benchmark campaign:

1. Solo-throughput variance — is there really a 4-8x spread across
   structurally similar SOLO worker runs?
2. Concurrency -> throughput — does effective tok/s collapse as
   ``dispatch_concurrency`` rises (the 2.17x KV-ceiling hypothesis)?
3. Prefix-cache vs concurrency — the smoking gun: does
   ``vllm_prefix_cache_hit_ratio`` fall and ``vllm_preemptions`` rise
   together with concurrency?
4. Controlled bench sweep — ``bench_run`` aggregate/per-stream tok/s and
   peak VRAM by ``concurrency`` (the WOR-221 / later sweep data).
5. Compaction interaction — do ``context_compactions`` co-occur with the
   slow runs?

Run::

    python scripts/spikes/wor336_throughput_forensic.py
    python scripts/spikes/wor336_throughput_forensic.py --db /path/to/app.db
    python scripts/spikes/wor336_throughput_forensic.py --json   # machine-readable

This is a read-only forensic. It opens the DB read-only and never writes.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
from collections.abc import Sequence
from typing import Any


def _default_db_path() -> str:
    """Best-effort location of the production metrics DB."""
    env = os.environ.get("REPO_SCAFFOLD_DB")
    if env and os.path.exists(env):
        return env
    candidates = [
        os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming", "repo-scaffold", "app.db"
        ),
        os.path.join(
            os.path.expanduser("~"), ".local", "share", "repo-scaffold", "app.db"
        ),
        os.path.join(os.path.expanduser("~"), ".repo-scaffold", "app.db"),
        "app.db",
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.getsize(c) > 0:
            return c
    return candidates[0]


def _connect_ro(path: str) -> sqlite3.Connection:
    """Open the DB strictly read-only via URI."""
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _spread(values: Sequence[float]) -> dict[str, float]:
    """Summary stats + max/min ratio for a list of throughputs."""
    vals = sorted(v for v in values if v is not None and v > 0)
    if not vals:
        return {}
    out: dict[str, float] = {
        "n": float(len(vals)),
        "min": round(vals[0], 3),
        "max": round(vals[-1], 3),
        "median": round(statistics.median(vals), 3),
        "mean": round(statistics.fmean(vals), 3),
    }
    if len(vals) >= 4:
        q = statistics.quantiles(vals, n=4)
        out["p25"] = round(q[0], 3)
        out["p75"] = round(q[2], 3)
    out["max_min_ratio"] = round(vals[-1] / vals[0], 2) if vals[0] > 0 else 0.0
    return out


def _concurrency_bucket(c: int | None) -> str:
    """Bucket a dispatch concurrency into solo / low / mid / high."""
    if c is None:
        return "unknown"
    if c <= 1:
        return "solo (<=1)"
    if c <= 2:
        return "2"
    if c <= 4:
        return "3-4"
    return "5+"


def analyse_solo_variance(con: sqlite3.Connection) -> dict[str, Any]:
    """Hypothesis 1 — spread of tok/s across SOLO worker runs."""
    rows = con.execute(
        """
        SELECT ticket_id, output_tokens_per_wall_second AS tps,
               local_wall_time AS wall, local_output_tokens AS out_tok,
               dispatch_concurrency AS conc, context_compactions AS comp
        FROM ticket_metrics
        WHERE local_used = 1
          AND output_tokens_per_wall_second IS NOT NULL
          AND output_tokens_per_wall_second > 0
          AND (dispatch_concurrency IS NULL OR dispatch_concurrency <= 1)
        ORDER BY tps
        """
    ).fetchall()
    tps = [r["tps"] for r in rows]
    return {
        "spread": _spread(tps),
        "slowest": [
            {
                "ticket": r["ticket_id"],
                "tps": round(r["tps"], 2),
                "wall_s": r["wall"],
                "out_tok": r["out_tok"],
                "compactions": r["comp"],
            }
            for r in rows[:5]
        ],
        "fastest": [
            {
                "ticket": r["ticket_id"],
                "tps": round(r["tps"], 2),
                "wall_s": r["wall"],
                "out_tok": r["out_tok"],
                "compactions": r["comp"],
            }
            for r in rows[-5:]
        ],
    }


def analyse_concurrency_throughput(con: sqlite3.Connection) -> dict[str, Any]:
    """Hypothesis 2 — effective tok/s by dispatch_concurrency bucket."""
    rows = con.execute(
        """
        SELECT dispatch_concurrency AS conc,
               output_tokens_per_wall_second AS tps
        FROM ticket_metrics
        WHERE local_used = 1
          AND output_tokens_per_wall_second IS NOT NULL
          AND output_tokens_per_wall_second > 0
        """
    ).fetchall()
    buckets: dict[str, list[float]] = {}
    for r in rows:
        buckets.setdefault(_concurrency_bucket(r["conc"]), []).append(r["tps"])
    return {b: _spread(v) for b, v in sorted(buckets.items(), key=lambda kv: kv[0])}


def analyse_prefix_cache(con: sqlite3.Connection) -> dict[str, Any]:
    """Hypothesis 3 — prefix-cache hit ratio & preemptions vs concurrency."""
    rows = con.execute(
        """
        SELECT dispatch_concurrency AS conc,
               vllm_prefix_cache_hit_ratio AS hit,
               vllm_preemptions AS preempt,
               output_tokens_per_wall_second AS tps,
               vllm_metrics_attributable AS attributable
        FROM ticket_metrics
        WHERE local_used = 1
          AND vllm_prefix_cache_hit_ratio IS NOT NULL
        """
    ).fetchall()
    by_bucket: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        b = _concurrency_bucket(r["conc"])
        d = by_bucket.setdefault(b, {"hit": [], "preempt": [], "tps": []})
        if r["hit"] is not None:
            d["hit"].append(r["hit"])
        if r["preempt"] is not None:
            d["preempt"].append(float(r["preempt"]))
        if r["tps"] is not None and r["tps"] > 0:
            d["tps"].append(r["tps"])
    summary = {}
    for b, d in sorted(by_bucket.items()):
        summary[b] = {
            "n": len(d["hit"]),
            "mean_prefix_hit_ratio": (
                round(statistics.fmean(d["hit"]), 4) if d["hit"] else None
            ),
            "mean_preemptions": (
                round(statistics.fmean(d["preempt"]), 2) if d["preempt"] else None
            ),
            "mean_tps": (round(statistics.fmean(d["tps"]), 2) if d["tps"] else None),
        }
    return {
        "rows_with_vllm_metrics": len(rows),
        "by_concurrency": summary,
    }


def analyse_bench_sweep(con: sqlite3.Connection) -> dict[str, Any]:
    """Hypothesis 4 — controlled bench_run tok/s & VRAM by concurrency."""
    rows = con.execute(
        """
        SELECT concurrency AS conc,
               COUNT(*) AS n,
               AVG(throughput_tok_s) AS mean_tps,
               MIN(throughput_tok_s) AS min_tps,
               MAX(throughput_tok_s) AS max_tps,
               AVG(peak_vram_gb) AS mean_vram,
               MAX(peak_vram_gb) AS max_vram,
               AVG(context_size) AS mean_ctx
        FROM bench_run
        WHERE outcome = 'ok' AND throughput_tok_s IS NOT NULL
        GROUP BY concurrency
        ORDER BY concurrency
        """
    ).fetchall()
    return {
        "by_concurrency": [
            {
                "concurrency": r["conc"],
                "n": r["n"],
                "mean_per_stream_tok_s": round(r["mean_tps"], 2),
                "min_tok_s": round(r["min_tps"], 2),
                "max_tok_s": round(r["max_tps"], 2),
                "implied_aggregate_tok_s": round(r["mean_tps"] * (r["conc"] or 1), 2),
                "mean_peak_vram_gb": (
                    round(r["mean_vram"], 2) if r["mean_vram"] is not None else None
                ),
                "max_peak_vram_gb": (
                    round(r["max_vram"], 2) if r["max_vram"] is not None else None
                ),
                "mean_ctx": int(r["mean_ctx"]) if r["mean_ctx"] is not None else None,
            }
            for r in rows
        ]
    }


def analyse_compaction(con: sqlite3.Connection) -> dict[str, Any]:
    """Hypothesis 5 — do compactions co-occur with the slow runs?"""
    rows = con.execute(
        """
        SELECT context_compactions AS comp,
               AVG(output_tokens_per_wall_second) AS mean_tps,
               COUNT(*) AS n
        FROM ticket_metrics
        WHERE local_used = 1
          AND output_tokens_per_wall_second IS NOT NULL
          AND output_tokens_per_wall_second > 0
          AND context_compactions IS NOT NULL
        GROUP BY (context_compactions > 0)
        """
    ).fetchall()
    out = {}
    for r in rows:
        key = "with_compaction" if (r["comp"] or 0) > 0 else "no_compaction"
        out[key] = {"n": r["n"], "mean_tps": round(r["mean_tps"], 2)}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WOR-336 throughput forensic")
    ap.add_argument("--db", default=_default_db_path(), help="path to app.db")
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db) or os.path.getsize(args.db) == 0:
        print(f"ERROR: metrics DB not found or empty at {args.db}")
        return 1

    con = _connect_ro(args.db)
    try:
        result = {
            "db": os.path.abspath(args.db),
            "h1_solo_variance": analyse_solo_variance(con),
            "h2_concurrency_throughput": analyse_concurrency_throughput(con),
            "h3_prefix_cache_vs_concurrency": analyse_prefix_cache(con),
            "h4_bench_concurrency_sweep": analyse_bench_sweep(con),
            "h5_compaction_interaction": analyse_compaction(con),
        }
    finally:
        con.close()

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"WOR-336 throughput forensic — {result['db']}\n")
    print("== H1: SOLO throughput variance ==")
    print(" ", result["h1_solo_variance"]["spread"])
    print("  slowest:", result["h1_solo_variance"]["slowest"])
    print("  fastest:", result["h1_solo_variance"]["fastest"])
    print("\n== H2: tok/s by dispatch_concurrency ==")
    for b, s in result["h2_concurrency_throughput"].items():
        print(f"  {b}: {s}")
    print("\n== H3: prefix-cache & preemptions vs concurrency ==")
    print(
        "  rows with vLLM metrics:",
        result["h3_prefix_cache_vs_concurrency"]["rows_with_vllm_metrics"],
    )
    for b, s in result["h3_prefix_cache_vs_concurrency"]["by_concurrency"].items():
        print(f"  {b}: {s}")
    print("\n== H4: controlled bench_run sweep ==")
    for r in result["h4_bench_concurrency_sweep"]["by_concurrency"]:
        print(f"  {r}")
    print("\n== H5: compaction interaction ==")
    for k, v in result["h5_compaction_interaction"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
