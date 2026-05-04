"""One-shot migration: copy metrics.db + bench.db into a single app.db.

How to run:
    python scripts/migrate_split_db_to_unified.py

What it copies:
    - From metrics.db:  ticket_metrics, check_run_log, ticket_run_log
    - From bench.db:    bench_run

    All data is copied via INSERT OR IGNORE to handle concurrent watcher writes
    safely. The operator should stop the watcher daemon before running this script.

Legacy files:
    This script does NOT delete metrics.db or bench.db. After verifying that
    app.db contains all expected data and is functioning correctly, delete the
    legacy files manually:

        rm ~/.config/repo-scaffold/metrics.db  # Linux/macOS
        rm ~/AppData/Roaming/repo-scaffold/metrics.db  # Windows

        rm ~/.config/repo-scaffold/bench.db  # Linux/macOS
        rm ~/AppData/Roaming/repo-scaffold/bench.db  # Windows

Idempotency:
    A second run on an already-migrated app.db is a no-op — the script checks
    whether the target tables exist in app.db and skips silently if they do.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _get_legacy_paths() -> tuple[Path, Path]:
    """Return (metrics_db_path, bench_db_path) from the platform config dir."""
    import platform

    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming"
    else:
        base = Path.home() / ".config"
    app_dir = base / "repo-scaffold"
    return app_dir / "metrics.db", app_dir / "bench.db"


def _get_target_path() -> Path:
    """Return the unified app.db path."""
    import platform

    if platform.system() == "Windows":
        base = Path.home() / "AppData" / "Roaming"
    else:
        base = Path.home() / ".config"
    return base / "repo-scaffold" / "app.db"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists in the database."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row[0] > 0


def _read_all_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """Read all rows from a table into memory."""
    return conn.execute(f"SELECT * FROM {table}").fetchall()  # nosemgrep


def _capture_table(conn: sqlite3.Connection, table: str) -> dict | None:
    """Capture DDL + rows + column names from a table.

    Returns dict with 'ddl', 'cols', and 'rows', or None.
    """
    ddl_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not ddl_row or not ddl_row[0]:
        return None
    pragma_sql = f"PRAGMA table_info({table})"  # nosemgrep
    cols = [row[1] for row in conn.execute(pragma_sql).fetchall()]  # nosemgrep
    rows = _read_all_rows(conn, table)
    return {"ddl": ddl_row[0], "cols": cols, "rows": rows}


def migrate(metrics_db: Path, bench_db: Path, target_db: Path) -> None:
    """Copy metrics.db and bench.db tables into a single app.db.

    Strategy: read from src first (closing src before touching dst),
    then write to dst in a single transaction. This avoids ATTACH-based
    locking issues entirely.
    """

    # --- Idempotency check: if target already has all tables, skip. ---
    target_db.parent.mkdir(parents=True, exist_ok=True)
    target_exists = target_db.exists()

    if target_exists:
        with sqlite3.connect(target_db) as conn:
            tables = {
                "ticket_metrics",
                "check_run_log",
                "ticket_run_log",
                "bench_run",
            }
            all_present = all(_table_exists(conn, t) for t in tables)
        if all_present:
            print("app.db already contains all tables — nothing to do.")
            return

    # --- Copy metrics.db tables ---
    if metrics_db.exists():
        with sqlite3.connect(metrics_db) as src:
            captured: dict[str, dict] = {}
            for table in ("ticket_metrics", "check_run_log", "ticket_run_log"):
                result = _capture_table(src, table)
                if result:
                    captured[table] = result

        # Now write to dst — src is fully closed
        with sqlite3.connect(target_db) as dst:
            dst.execute("BEGIN IMMEDIATE")

            for table, info in captured.items():
                if not _table_exists(dst, table):
                    dst.execute(info["ddl"])
                if info["rows"]:
                    # Defensive: handle schema drift between source and target.
                    # If the source DB has columns the target doesn't (e.g. an
                    # earlier code version added columns later removed), only
                    # copy the intersection. Phantom columns get logged + dropped.
                    src_cols = info["cols"]
                    dst_pragma = f"PRAGMA table_info({table})"  # nosemgrep
                    dst_cols = {
                        row[1]
                        for row in dst.execute(dst_pragma).fetchall()  # nosemgrep
                    }
                    common_cols = [c for c in src_cols if c in dst_cols]
                    skipped = [c for c in src_cols if c not in dst_cols]
                    if skipped:
                        print(
                            f"  {table}: skipping {len(skipped)} column(s) not in "
                            f"target schema: {', '.join(skipped)}"
                        )
                    if not common_cols:
                        continue

                    idx_map = [src_cols.index(c) for c in common_cols]
                    col_list = ", ".join(common_cols)
                    placeholders = ", ".join("?" for _ in common_cols)
                    for row in info["rows"]:
                        projected = tuple(row[i] for i in idx_map)
                        dst.execute(
                            f"INSERT OR IGNORE INTO {table} ({col_list}) "
                            f"VALUES ({placeholders})",
                            projected,
                        )

            dst.commit()

        for table in ("ticket_metrics", "check_run_log", "ticket_run_log"):
            if table in captured:
                print(f"  {table}: {len(captured[table]['rows'])} rows")
    else:
        print(f"  metrics.db not found at {metrics_db} — skipping.")

    # --- Copy bench.db tables ---
    if bench_db.exists():
        with sqlite3.connect(bench_db) as src:
            bench_info = _capture_table(src, "bench_run")

        if bench_info:
            with sqlite3.connect(target_db) as dst:
                dst.execute("BEGIN IMMEDIATE")
                dst.execute(bench_info["ddl"])
                if bench_info["rows"]:
                    col_list = ", ".join(bench_info["cols"])
                    placeholders = ", ".join("?" for _ in bench_info["cols"])
                    for row in bench_info["rows"]:
                        dst.execute(
                            f"INSERT OR IGNORE INTO bench_run ({col_list}) "
                            f"VALUES ({placeholders})",
                            row,
                        )
                dst.commit()

            print(f"  bench_run: {len(bench_info['rows'])} rows")
        else:
            print("  bench_run table not found in bench.db — skipping.")
    else:
        print(f"  bench.db not found at {bench_db} — skipping.")

    # --- Summary ---
    print()
    print(f"Migration complete. Unified database: {target_db}")
    print("Legacy files (metrics.db, bench.db) are left in place for manual deletion.")


def main() -> None:
    metrics_db, bench_db = _get_legacy_paths()
    target_db = _get_target_path()

    print(f"Source: metrics.db = {metrics_db}")
    print(f"Source: bench.db   = {bench_db}")
    print(f"Target: app.db     = {target_db}")
    print()
    print("Copying tables...")

    migrate(metrics_db, bench_db, target_db)


if __name__ == "__main__":
    main()
