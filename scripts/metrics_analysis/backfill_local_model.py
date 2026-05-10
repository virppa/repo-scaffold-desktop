"""Backfill ``ticket_metrics.local_model`` rows labelled ``claude-sonnet-4-6``
to ``qwen3-coder``.

The ``_LOCAL_MODEL`` constant in ``app.core.watcher.watcher_types`` was
incorrectly set to ``'claude-sonnet-4-6'`` (the cloud tier label) instead of
``'qwen3-coder'`` (the actual ``--served-model-name`` passed to vLLM).  All
historical rows that recorded local input tokens carry the wrong label.

This script scans ``ticket_metrics`` and updates the ``local_model`` column
for rows where ``local_input_tokens IS NOT NULL`` and the current value is
``'claude-sonnet-4-6'``.

Usage (from repo root)::

    python scripts/metrics_analysis/backfill_local_model.py           # dry-run
    python scripts/metrics_analysis/backfill_local_model.py --apply   # write

WOR-424.
"""

from __future__ import annotations

import argparse
import sqlite3

from app.core.metrics import MetricsStore  # noqa: E402

_TARGET = "qwen3-coder"
_MISLABELLED = "claude-sonnet-4-6"

_SELECT_SQL = (
    "SELECT ticket_id, project_id, local_model, local_input_tokens "
    "FROM ticket_metrics "
    "WHERE local_model = ? AND local_input_tokens IS NOT NULL"
)

_UPDATE_SQL = (
    "UPDATE ticket_metrics "
    "SET local_model = ? "
    "WHERE ticket_id = ? AND project_id = ? AND local_model = ?"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0].strip())
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the UPDATEs (default is dry-run).",
    )
    ap.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Override the SQLite DB path. Defaults to MetricsStore.get_db_path().",
    )
    args = ap.parse_args()

    db_path = args.db_path or MetricsStore.get_db_path()
    print(f"DB:        {db_path}")
    print(f"Mode:      {'APPLY' if args.apply else 'dry-run'}")
    print()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(_SELECT_SQL, (_MISLABELLED,)).fetchall()
    rows_total_local = len(rows)
    rows_already_correct = 0
    rows_skipped_other_model = 0

    # Count rows that are already the correct value (shouldn't match the
    # WHERE clause, but defensive counting keeps summary complete).
    already_correct = cur.execute(
        "SELECT COUNT(*) FROM ticket_metrics "
        "WHERE local_model = ? AND local_input_tokens IS NOT NULL",
        (_TARGET,),
    ).fetchone()[0]
    rows_already_correct = already_correct

    rows_updated = 0
    rows_skipped_other_model = 0  # placeholders — the SELECT already filters

    for row in rows:
        ticket_id = row["ticket_id"]
        project_id = row["project_id"]
        current_model = row["local_model"]

        if current_model == _TARGET:
            # Should not appear in results (WHERE clause), but defensive.
            rows_already_correct += 1
            print(f"  {ticket_id:<10} project={project_id:<22} already={current_model}")
            continue

        print(
            f"  {ticket_id:<10} project={project_id:<22} {current_model} -> {_TARGET}"
        )
        if args.apply:
            cur.execute(_UPDATE_SQL, (_TARGET, ticket_id, project_id, _MISLABELLED))
            rows_updated += 1

    if args.apply:
        conn.commit()

    # Count rows with a third model value for the 4th summary bucket.
    rows_skipped_other_model = cur.execute(
        "SELECT COUNT(*) FROM ticket_metrics "
        "WHERE local_model != ? "
        "AND local_model != ? "
        "AND local_input_tokens IS NOT NULL",
        (_TARGET, _MISLABELLED),
    ).fetchone()[0]

    print()
    print("=== Summary ===")
    suffix = "" if args.apply else " (dry-run)"
    print(f"  rows_total_local   : {rows_total_local}{suffix}")
    print(f"  rows_already_correct: {rows_already_correct}")
    print(f"  rows_updated       : {rows_updated}{suffix}")
    print(f"  rows_skipped_other : {rows_skipped_other_model}")

    if not args.apply and rows_updated == 0:
        print()
        print("No rows to update. Re-run with --apply to commit.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
