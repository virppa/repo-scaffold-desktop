"""Backfill the WOR-380 per-worker behavior columns from archived worker logs.

The worker stream-json log files for past tickets persist at
``.claude/artifacts/<ticket_slug>/worker_<ticket_id_lower>.log``. Running
``_parse_worker_behavior`` over them retroactively populates the 9
columns this PR added, for the 30+ tickets recorded before the parser
was wired into ``finalize_worker``.

WOR-370 (vLLM /metrics deltas) cannot be backfilled — those snapshots
are server-side counters that needed to be captured at dispatch/reap
time and weren't. The vllm_* columns stay NULL on historical rows.

Usage (from repo root):

    python scripts/metrics_analysis/backfill_worker_behavior.py           # dry-run
    python scripts/metrics_analysis/backfill_worker_behavior.py --apply   # write
    python scripts/metrics_analysis/backfill_worker_behavior.py --apply --force
        # overwrite existing non-NULL values too (default skips rows that already
        # have non-NULL behavior data)

Idempotent in default mode: only UPDATEs rows where every WOR-380 column
is currently NULL. Re-running is a no-op once everything is filled in.
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
from app.core.watcher.watcher_helpers import (  # noqa: E402
    WorkerBehavior,
    _parse_worker_behavior,
)

# Columns to populate. Matches TicketMetrics field names. The two SQL
# statements below are derived from this tuple but written as static
# strings (no f-string composition at call time) so static analysers
# can't mistake them for dynamic SQL.
_BEHAVIOR_COLUMNS = (
    "turn_count",
    "tool_calls_total",
    "tool_calls_breakdown",
    "thinking_blocks",
    "thinking_chars_total",
    "input_tokens_max",
    "input_tokens_first",
    "input_tokens_last",
    "redundant_reads_count",
)

_SELECT_SQL = (
    "SELECT project_id, "
    "turn_count, tool_calls_total, tool_calls_breakdown, "
    "thinking_blocks, thinking_chars_total, "
    "input_tokens_max, input_tokens_first, input_tokens_last, "
    "redundant_reads_count "
    "FROM ticket_metrics WHERE ticket_id = ?"
)

_UPDATE_SQL = (
    "UPDATE ticket_metrics SET "
    "turn_count = ?, tool_calls_total = ?, tool_calls_breakdown = ?, "
    "thinking_blocks = ?, thinking_chars_total = ?, "
    "input_tokens_max = ?, input_tokens_first = ?, input_tokens_last = ?, "
    "redundant_reads_count = ? "
    "WHERE ticket_id = ? AND project_id = ?"
)


def _ticket_id_from_log_name(log_path: Path) -> str | None:
    """Extract WOR-NNN from a worker log filename, e.g. ``worker_wor-282.log``."""
    stem = log_path.stem  # "worker_wor-282"
    if not stem.startswith("worker_"):
        return None
    suffix = stem[len("worker_") :]
    return suffix.upper() if suffix else None


def _columns_nonnull(row: sqlite3.Row) -> bool:
    """True if any of the WOR-380 columns is already populated on this row."""
    return any(row[c] is not None for c in _BEHAVIOR_COLUMNS)


def _to_update_values(behavior: WorkerBehavior) -> tuple[object, ...]:
    breakdown_json = (
        json.dumps(behavior.tool_calls_breakdown, sort_keys=True)
        if behavior.tool_calls_breakdown is not None
        else None
    )
    return (
        behavior.turn_count,
        behavior.tool_calls_total,
        breakdown_json,
        behavior.thinking_blocks,
        behavior.thinking_chars_total,
        behavior.input_tokens_max,
        behavior.input_tokens_first,
        behavior.input_tokens_last,
        behavior.redundant_reads_count,
    )


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
        help="Overwrite existing non-NULL behavior values (default skips them).",
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
    print(f"Worker logs found: {len(log_files)}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    updated = 0
    skipped_already_set = 0
    skipped_no_row = 0
    skipped_unparseable = 0

    for log_path in log_files:
        ticket_id = _ticket_id_from_log_name(log_path)
        if ticket_id is None:
            continue

        rows = cur.execute(_SELECT_SQL, (ticket_id,)).fetchall()
        if not rows:
            skipped_no_row += 1
            continue

        # Some tickets have multiple project_id rows (cross-project). Backfill each.
        for row in rows:
            if not args.force and _columns_nonnull(row):
                skipped_already_set += 1
                continue

            behavior = _parse_worker_behavior(log_path)
            if behavior.turn_count is None:
                # Unparseable — sentinel says all None; nothing useful to write.
                skipped_unparseable += 1
                continue

            values = _to_update_values(behavior) + (ticket_id, row["project_id"])
            print(
                f"  {ticket_id:<10} project={row['project_id']:<22} "
                f"turns={behavior.turn_count:>3} tools={behavior.tool_calls_total:>3} "
                f"think={behavior.thinking_blocks:>3} "
                f"redundant_reads={behavior.redundant_reads_count}"
            )
            if args.apply:
                cur.execute(_UPDATE_SQL, values)
                updated += 1

    if args.apply:
        conn.commit()

    print()
    print("=== Summary ===")
    suffix = "" if args.apply else " (dry-run)"
    print(f"  rows updated         : {updated}{suffix}")
    print(f"  skipped (already set): {skipped_already_set}")
    print(f"  skipped (no DB row)  : {skipped_no_row}")
    print(f"  skipped (unparseable): {skipped_unparseable}")
    if not args.apply and updated == 0:
        eligible = sum(
            1
            for log_path in log_files
            if _ticket_id_from_log_name(log_path)
            and cur.execute(
                "SELECT 1 FROM ticket_metrics WHERE ticket_id = ?",
                (_ticket_id_from_log_name(log_path),),
            ).fetchone()
        )
        print(f"  (eligible rows in DB : {eligible})")
        print()
        print("Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
