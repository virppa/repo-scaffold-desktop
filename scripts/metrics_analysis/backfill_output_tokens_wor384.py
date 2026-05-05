"""Backfill local_output_tokens (and derived columns) from WOR-384 fix.

Tickets recorded after WOR-368 (vLLM-direct path) and before WOR-384 wrote
``local_output_tokens = 0`` because vLLM 0.20's Anthropic Messages API
emits ``usage.output_tokens: 0`` in every assistant event. WOR-384 adds a
content-block character estimate fallback to ``_parse_worker_usage``.
This script re-runs the parser against preserved worker logs and updates:

  - local_output_tokens   — primary fix
  - local_tokens          — = local_input_tokens + local_output_tokens
  - output_tokens_per_wall_second  — = local_output_tokens / local_wall_time

Sibling to ``backfill_worker_behavior.py`` (WOR-380). Same idempotency
contract: only updates rows where ``local_output_tokens = 0`` (which is
the regression sentinel — non-zero means either Anthropic-shape worker
or a row already backfilled).

Usage (from repo root):

    python scripts/metrics_analysis/backfill_output_tokens_wor384.py           # dry-run
    python scripts/metrics_analysis/backfill_output_tokens_wor384.py --apply
    python scripts/metrics_analysis/backfill_output_tokens_wor384.py --apply --force
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.metrics import MetricsStore  # noqa: E402
from app.core.watcher.watcher_helpers import _parse_worker_usage  # noqa: E402

_SELECT_SQL = (
    "SELECT project_id, local_input_tokens, local_output_tokens, "
    "local_wall_time FROM ticket_metrics WHERE ticket_id = ?"
)

_UPDATE_SQL = (
    "UPDATE ticket_metrics SET "
    "local_output_tokens = ?, local_tokens = ?, "
    "output_tokens_per_wall_second = ? "
    "WHERE ticket_id = ? AND project_id = ?"
)


def _ticket_id_from_log_name(log_path: Path) -> str | None:
    """Extract WOR-NNN from a worker log filename, e.g. ``worker_wor-282.log``."""
    stem = log_path.stem
    if not stem.startswith("worker_"):
        return None
    suffix = stem[len("worker_") :]
    return suffix.upper() if suffix else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the UPDATEs (default dry-run).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Update even when local_output_tokens is already non-zero.",
    )
    ap.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = ap.parse_args()

    db_path = MetricsStore.get_db_path()
    print(f"DB:        {db_path}")
    print(f"Logs root: {args.repo_root / '.claude' / 'artifacts'}")
    print(f"Mode:      {'APPLY' if args.apply else 'dry-run'}")
    print(f"Force:     {args.force}")
    print()

    artifacts_root = args.repo_root / ".claude" / "artifacts"
    if not artifacts_root.exists():
        print(f"ERROR: {artifacts_root} not found.", file=sys.stderr)
        return 1

    log_files = sorted(artifacts_root.glob("*/worker_wor-*.log"))
    print(f"Worker logs found: {len(log_files)}\n")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    updated = 0
    skipped_already_set = 0
    skipped_no_row = 0
    skipped_zero_estimate = 0

    for log_path in log_files:
        ticket_id = _ticket_id_from_log_name(log_path)
        if ticket_id is None:
            continue

        rows = cur.execute(_SELECT_SQL, (ticket_id,)).fetchall()
        if not rows:
            skipped_no_row += 1
            continue

        for row in rows:
            current_output = row["local_output_tokens"]
            if not args.force and current_output and current_output > 0:
                skipped_already_set += 1
                continue

            input_tok, output_tok, _, _ = _parse_worker_usage(log_path)
            if not output_tok or output_tok <= 0:
                skipped_zero_estimate += 1
                continue

            input_val = row["local_input_tokens"] or input_tok or 0
            total = input_val + output_tok
            wall = row["local_wall_time"] or 0.0
            rate = (output_tok / wall) if wall and wall > 0 else 0.0

            print(
                f"  {ticket_id:<10} project={row['project_id']:<22} "
                f"out: {current_output} -> {output_tok:>6}  total: {total:>10,}  "
                f"rate: {rate:>5.1f} tok/s"
            )
            if args.apply:
                cur.execute(
                    _UPDATE_SQL,
                    (output_tok, total, rate, ticket_id, row["project_id"]),
                )
                updated += 1

    if args.apply:
        conn.commit()

    print()
    print("=== Summary ===")
    suffix = "" if args.apply else " (dry-run)"
    print(f"  rows updated           : {updated}{suffix}")
    print(f"  skipped (already non-0): {skipped_already_set}")
    print(f"  skipped (no DB row)    : {skipped_no_row}")
    print(f"  skipped (zero estimate): {skipped_zero_estimate}")
    if not args.apply:
        print()
        print("Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
