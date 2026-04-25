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


def _compare(id1: str, id2: str, db_path: Path) -> None:
    from scripts.bench import reporter

    rows1 = reporter.load_sweep(db_path, id1)
    rows2 = reporter.load_sweep(db_path, id2)
    reporter.print_compare_table(rows1, rows2, id1, id2)


# ── CLI ───────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local LLM benchmark runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="bench.toml",
        help="Path to bench.toml (default: bench.toml)",
    )
    parser.add_argument(
        "--tier",
        default=None,
        help="Filter by tier: speed|coding|prefill_shared|prefill_unshared|boundary",
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
        help="Create scripts/bench/fixtures/project_summary_50k.txt",
    )
    parser.add_argument(
        "--browse",
        action="store_true",
        help="Open bench.db in datasette browser",
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
        _compare(args.compare[0], args.compare[1], db_path)
        return 0

    run(
        args.config,
        db_path,
        tier=args.tier,
        resume=args.resume,
        output_json=args.output_json,
        output_csv=args.output_csv,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
