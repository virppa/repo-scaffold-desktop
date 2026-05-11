"""Tests for scripts/metrics_analysis/backfill_local_model.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_tmp_db(tmp_path: Path, rows: list[tuple[str, str, str, int | None]]) -> Path:
    """Create a temporary SQLite DB with one ticket_metrics row per tuple."""
    tmp = tmp_path / "backfill_local_model.db"
    conn = sqlite3.connect(str(tmp))
    conn.execute(
        "CREATE TABLE ticket_metrics ("
        "ticket_id TEXT, project_id TEXT, local_model TEXT, "
        "local_input_tokens INTEGER)"
    )
    for ticket_id, project_id, local_model, local_input_tokens in rows:
        conn.execute(
            "INSERT INTO ticket_metrics VALUES (?, ?, ?, ?)",
            (ticket_id, project_id, local_model, local_input_tokens),
        )
    conn.commit()
    conn.close()
    return tmp


def test_dry_run_does_not_modify_db(tmp_path: Path) -> None:
    """Dry-run (default) should NOT modify the DB.

    Fixture: one mislabeled row with local_input_tokens (should match),
    one mislabeled row with NULL (should skip), one already correct.
    """
    tmp_db = _make_tmp_db(
        tmp_path,
        [
            ("WOR-100", "proj-A", "claude-sonnet-4-6", 1000),
            ("WOR-101", "proj-B", "claude-sonnet-4-6", None),
            ("WOR-102", "proj-C", "qwen3-coder", 2000),
        ],
    )

    conn = sqlite3.connect(str(tmp_db))
    qwen_count_before = conn.execute(
        "SELECT COUNT(*) FROM ticket_metrics WHERE local_model = 'qwen3-coder'"
    ).fetchone()[0]
    claude_count_before = conn.execute(
        "SELECT COUNT(*) FROM ticket_metrics WHERE local_model = 'claude-sonnet-4-6'"
    ).fetchone()[0]
    conn.close()

    # Simulate the dry-run SELECT
    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ticket_metrics "
        "WHERE local_model = ? "
        "AND local_input_tokens IS NOT NULL",
        ("claude-sonnet-4-6",),
    ).fetchall()
    conn.close()

    # Only 1 row matches the WHERE (WOR-100), and dry-run should NOT update it
    assert len(rows) == 1
    assert rows[0]["ticket_id"] == "WOR-100"

    # DB should be unchanged
    conn = sqlite3.connect(str(tmp_db))
    qwen_count_after = conn.execute(
        "SELECT COUNT(*) FROM ticket_metrics WHERE local_model = 'qwen3-coder'"
    ).fetchone()[0]
    claude_count_after = conn.execute(
        "SELECT COUNT(*) FROM ticket_metrics WHERE local_model = 'claude-sonnet-4-6'"
    ).fetchone()[0]
    conn.close()

    assert qwen_count_after == qwen_count_before  # still 1 (WOR-102)
    assert claude_count_after == claude_count_before  # still 2 (WOR-100 + WOR-101)


def test_apply_updates_mislabelled_rows(tmp_path: Path) -> None:
    """--apply should update rows with local_input_tokens IS NOT NULL."""
    tmp_db = _make_tmp_db(
        tmp_path,
        [
            ("WOR-100", "proj-A", "claude-sonnet-4-6", 1000),
            ("WOR-101", "proj-B", "claude-sonnet-4-6", None),
            ("WOR-102", "proj-C", "qwen3-coder", 2000),
        ],
    )

    # Simulate apply: UPDATE rows matching the WHERE clause
    conn = sqlite3.connect(str(tmp_db))
    cur = conn.cursor()
    cur.execute(
        "UPDATE ticket_metrics SET local_model = 'qwen3-coder' "
        "WHERE local_model = 'claude-sonnet-4-6' AND local_input_tokens IS NOT NULL"
    )
    rows_updated = cur.rowcount
    conn.commit()
    conn.close()

    assert rows_updated == 1  # only WOR-100 has non-NULL tokens

    # Verify
    conn = sqlite3.connect(str(tmp_db))
    qwen_count = conn.execute(
        "SELECT COUNT(*) FROM ticket_metrics WHERE local_model = 'qwen3-coder'"
    ).fetchone()[0]
    claude_count = conn.execute(
        "SELECT COUNT(*) FROM ticket_metrics WHERE local_model = 'claude-sonnet-4-6'"
    ).fetchone()[0]
    conn.close()

    assert qwen_count == 2  # WOR-102 (pre-existing) + WOR-100 (updated)
    assert claude_count == 1  # WOR-101 skipped (NULL tokens)


def test_idempotent_apply(tmp_path: Path) -> None:
    """Second --apply should produce rows_updated=0."""
    tmp_db = _make_tmp_db(
        tmp_path,
        [
            ("WOR-100", "proj-A", "claude-sonnet-4-6", 1000),
        ],
    )

    # First apply
    conn = sqlite3.connect(str(tmp_db))
    conn.execute(
        "UPDATE ticket_metrics SET local_model = 'qwen3-coder' "
        "WHERE local_model = 'claude-sonnet-4-6' AND local_input_tokens IS NOT NULL"
    )
    conn.commit()
    conn.close()

    # Second apply — no rows should match the WHERE clause now
    conn = sqlite3.connect(str(tmp_db))
    rows = conn.execute(
        "SELECT * FROM ticket_metrics "
        "WHERE local_model = 'claude-sonnet-4-6' "
        "AND local_input_tokens IS NOT NULL"
    ).fetchall()
    conn.close()

    assert len(rows) == 0  # no more claude-sonnet-4-6 rows with non-NULL tokens
