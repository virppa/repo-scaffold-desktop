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
import subprocess  # nosec B404
import sys
from pathlib import Path

# Ensure repo root is on sys.path when run as `python scripts/bench/run_bench.py`
sys.path.insert(0, str(Path(__file__).parents[2]))

from app.core.bench_store import BenchStore
from scripts.bench.fixtures import generate_fixtures
from scripts.bench.runner import run

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


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


def _compare(
    id1: str, id2: str, db_path: Path, *, regression_threshold_pct: float
) -> bool:
    from scripts.bench import reporter

    rows1 = reporter.load_sweep(db_path, id1)
    rows2 = reporter.load_sweep(db_path, id2)
    return reporter.print_compare_table(
        rows1,
        rows2,
        id1,
        id2,
        regression_threshold_pct=regression_threshold_pct,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local LLM benchmark runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="config/bench.toml",
        help="Path to bench.toml (default: config/bench.toml)",
    )
    parser.add_argument(
        "--tier",
        default=None,
        help="Filter by tier: speed|coding|prefill_shared|prefill_unshared|boundary",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL_ID",
        help="Run only cases matching this model id (e.g. qwen3:14b)",
    )
    parser.add_argument(
        "--backend",
        default=None,
        metavar="BACKEND_ID",
        help="Run only cases matching this backend id (e.g. local_qwen)",
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
        help="Create scripts/bench/fixtures/prefill_50k.txt",
    )
    parser.add_argument(
        "--browse",
        action="store_true",
        help="Open bench.db in datasette browser",
    )
    parser.add_argument(
        "--regression-threshold",
        type=float,
        default=10.0,
        metavar="PCT",
        help="Regression threshold %% for --compare (default: 10)",
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

    db_path = Path(args.db_path) if args.db_path else BenchStore.get_db_path()

    if args.generate_fixtures:
        generate_fixtures()
        return 0

    if args.browse:
        _browse(db_path)
        return 0

    if args.compare:
        regression = _compare(
            args.compare[0],
            args.compare[1],
            db_path,
            regression_threshold_pct=args.regression_threshold,
        )
        return 1 if regression else 0

    run(
        args.config,
        db_path,
        tier=args.tier,
        model=args.model,
        backend=args.backend,
        resume=args.resume,
        output_json=args.output_json,
        output_csv=args.output_csv,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
