"""One-shot backfill for the 9 behavior-telemetry columns in ticket_metrics.

Reads the stream-json artifact log for every ticket in the metrics DB,
calls ``_parse_worker_behavior`` to extract telemetry, and updates any
rows whose ``turn_count`` is NULL (the canonical "not-yet-parsed" marker).

Usage::

    python -m scripts.backfill_behavior_telemetry

Defaults to ``~/AppData/Roaming/repo-scaffold/app.db`` on Windows
and ``~/.config/repo-scaffold/app.db`` on POSIX. Override via
``--db-path``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

from app.core.metrics import MetricsStore
from app.core.watcher.watcher_helpers import _parse_worker_behavior

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill behaviour-telemetry columns for existing ticket_metrics rows."
        ),
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite DB path (default: auto-detect via MetricsStore.get_db_path).",
    )
    args = parser.parse_args()

    db_path = args.db_path or MetricsStore.get_db_path()
    if not db_path.exists():
        logger.error("DB not found at %s — nothing to backfill.", db_path)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    # Use the established log path pattern from the codebase
    # (<repo_root>/.claude/worker_<ticket_lower>.log).
    repo_root = Path.cwd()

    rows = conn.execute(
        "SELECT ticket_id FROM ticket_metrics WHERE turn_count IS NULL"
    ).fetchall()

    rows_total = len(rows)
    rows_updated = 0
    rows_logfile_missing = 0
    rows_parse_failed = 0

    for (ticket_id,) in rows:
        log_path = repo_root / ".claude" / f"worker_{ticket_id.lower()}.log"
        if not log_path.exists():
            rows_logfile_missing += 1
            continue

        behavior = _parse_worker_behavior(log_path)
        if behavior.turn_count is None:
            # Log was readable but had no assistant turns, or parse error.
            # turn_count==0 with zero-values means the log was readable
            # but had no turns — still valid, but for idempotency we skip
            # rows that were deliberately NULL (no turns → no behavior).
            # Actually, empty_readable returns turn_count=0, not None, so
            # this branch means parse failed.
            rows_parse_failed += 1
            continue

        # Build a minimal update — only the 9 behaviour columns.
        tool_breakdown_json: str | None = (
            json.dumps(behavior.tool_calls_breakdown, sort_keys=True)
            if behavior.tool_calls_breakdown is not None
            else None
        )
        conn.execute(
            """
            UPDATE ticket_metrics
               SET turn_count               = ?,
                   tool_calls_total         = ?,
                   tool_calls_breakdown     = ?,
                   thinking_blocks          = ?,
                   thinking_chars_total     = ?,
                   input_tokens_max         = ?,
                   input_tokens_first       = ?,
                   input_tokens_last        = ?,
                   redundant_reads_count    = ?
             WHERE ticket_id = ? AND turn_count IS NULL
            """,
            (
                behavior.turn_count,
                behavior.tool_calls_total,
                tool_breakdown_json,
                behavior.thinking_blocks,
                behavior.thinking_chars_total,
                behavior.input_tokens_max,
                behavior.input_tokens_first,
                behavior.input_tokens_last,
                behavior.redundant_reads_count,
                ticket_id,
            ),
        )
        if conn.total_changes > rows_updated:
            rows_updated += 1

    conn.close()

    print(
        f"rows_total={rows_total}, rows_updated={rows_updated}, "
        f"rows_logfile_missing={rows_logfile_missing}, "
        f"rows_parse_failed={rows_parse_failed}"
    )


if __name__ == "__main__":
    main()
