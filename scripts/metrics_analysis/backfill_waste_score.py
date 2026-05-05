"""Backfill waste_score and waste_breakdown_json from archived worker logs (WOR-393).

`waste_score` was added by WOR-277 (PR #665) and the parser was fixed by
WOR-349 ("signal always 0"). Rows recorded between those dates have NULL
waste_score; rows before WOR-277 don't have the column at all (additive
schema means NULL on old rows).

This script re-runs ``compute_waste_score`` against preserved worker logs
and updates the ``ticket_metrics`` rows. Sibling chore to:
- ``backfill_worker_behavior.py`` (WOR-380 columns)
- ``backfill_output_tokens_wor384.py`` (output_tokens estimate)

Usage (from repo root):

    python scripts/metrics_analysis/backfill_waste_score.py           # dry-run
    python scripts/metrics_analysis/backfill_waste_score.py --apply   # write
    python scripts/metrics_analysis/backfill_waste_score.py --apply --force
        # overwrite existing non-NULL waste_score values

Idempotent in default mode: only UPDATEs rows where waste_score is NULL.
Re-running is a no-op once everything is filled in.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.metrics import MetricsStore  # noqa: E402
from app.core.watcher.worker_waste import (  # noqa: E402
    WasteReport,
    compute_waste_score,
)

_SELECT_SQL = (
    "SELECT project_id, waste_score, waste_breakdown_json "
    "FROM ticket_metrics WHERE ticket_id = ?"
)

_UPDATE_SQL = (
    "UPDATE ticket_metrics SET waste_score = ?, waste_breakdown_json = ? "
    "WHERE ticket_id = ? AND project_id = ?"
)


def _ticket_id_from_log_name(log_path: Path) -> str | None:
    """Extract WOR-NNN from a worker log filename, e.g. ``worker_wor-282.log``."""
    stem = log_path.stem
    if not stem.startswith("worker_"):
        return None
    suffix = stem[len("worker_") :]
    return suffix.upper() if suffix else None


def _to_update_values(report: WasteReport) -> tuple[int, str]:
    breakdown_json = json.dumps(report.breakdown, sort_keys=True)
    return report.score, breakdown_json


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the UPDATEs (default is dry-run).",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing non-NULL waste_score values (default skips them).",
    )
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help="Repo root to scan for .claude/artifacts/. Defaults to git root.",
    )
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
    skipped_zero_score = 0

    for log_path in log_files:
        ticket_id = _ticket_id_from_log_name(log_path)
        if ticket_id is None:
            continue

        rows = cur.execute(_SELECT_SQL, (ticket_id,)).fetchall()
        if not rows:
            skipped_no_row += 1
            continue

        for row in rows:
            current = row["waste_score"]
            if not args.force and current is not None:
                skipped_already_set += 1
                continue

            report = compute_waste_score(log_path)
            # WasteReport returns score=0 when the log is missing/unreadable,
            # which is an unhelpful row. Skip writing zero-score reports
            # unless --force (the user explicitly wants every row recomputed).
            if report.score == 0 and not args.force:
                skipped_zero_score += 1
                continue

            score, breakdown_json = _to_update_values(report)
            print(
                f"  {ticket_id:<10} project={row['project_id']:<22} "
                f"score={score:>3} breakdown={breakdown_json}"
            )
            if args.apply:
                cur.execute(
                    _UPDATE_SQL,
                    (score, breakdown_json, ticket_id, row["project_id"]),
                )
                updated += 1

    if args.apply:
        conn.commit()

    print()
    print("=== Summary ===")
    suffix = "" if args.apply else " (dry-run)"
    print(f"  rows updated         : {updated}{suffix}")
    print(f"  skipped (already set): {skipped_already_set}")
    print(f"  skipped (no DB row)  : {skipped_no_row}")
    print(f"  skipped (zero score) : {skipped_zero_score}")
    if not args.apply and updated == 0:
        print()
        print("Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
