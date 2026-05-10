"""Tests for scripts/backfill_behavior_telemetry.py.

Exercise the backfill logic against a fixture log + temp SQLite DB:
populates a row from a known log, idempotent re-run, missing-log row
stays NULL.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from app.core.metrics import MetricsStore
from scripts.backfill_behavior_telemetry import main


def _make_test_db(tmp_path: Path) -> tuple[Path, MetricsStore]:
    """Create a temp SQLite DB with the ticket_metrics schema and return it."""
    db_path = tmp_path / "app.db"
    store = MetricsStore(db_path=db_path)
    return db_path, store


def _write_worker_log(path: Path, lines: list[dict[str, object]]) -> Path:
    """Write a stream-json worker log and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _assistant_turn(
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    content: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a single assistant-turn JSONL line."""
    msg: dict[str, object] = {"content": content or []}
    if input_tokens is not None or output_tokens is not None:
        usage: dict[str, int] = {}
        if input_tokens is not None:
            usage["input_tokens"] = input_tokens
        if output_tokens is not None:
            usage["output_tokens"] = output_tokens
        msg["usage"] = usage
    return {"type": "assistant", "message": msg}


class TestBackfillPopulatesNULLRows:
    """The backfill script populates the 9 behaviour columns for rows
    whose ``turn_count`` is NULL and whose artifact log exists on disk."""

    def test_populates_row_from_known_log(self, tmp_path: Path) -> None:
        """A ticket with a NULL turn_count and an existing log gets
        backfilled with parsed values."""
        db_path, store = _make_test_db(tmp_path)

        # Insert a NULL-row into the ticket_metrics table.
        with store._connect() as conn:
            conn.execute(
                """
                INSERT INTO ticket_metrics (
                    ticket_id, project_id, epic_id, implementation_mode,
                    outcome
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "WOR-900",
                    "proj-test",
                    "WOR-900",
                    "local",
                    "success",
                ),
            )

        # Create a fixture log with 2 assistant turns.
        log_dir = tmp_path / ".claude"
        log_path = log_dir / "worker_wor-900.log"
        _write_worker_log(
            log_path,
            [
                _assistant_turn(input_tokens=1000, content=[]),
                _assistant_turn(
                    input_tokens=1200,
                    content=[
                        {
                            "type": "thinking",
                            "thinking": "Let me work through this.",
                        },
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "foo.py"},
                        },
                    ],
                ),
            ],
        )

        # Patch get_db_path and cwd so the backfill finds our DB + log.
        with (
            patch.object(MetricsStore, "get_db_path", return_value=db_path),
            patch(
                "scripts.backfill_behavior_telemetry.Path.cwd", return_value=tmp_path
            ),
        ):
            main([])

        # Verify the row was updated.
        result = store.get_by_ticket("WOR-900", "proj-test")
        assert result is not None
        assert result.turn_count == 2
        assert result.thinking_blocks == 1
        assert result.input_tokens_first == 1000
        assert result.input_tokens_last == 1200
        assert result.input_tokens_max == 1200

    def test_idempotent_rerun_reports_zero_updates(self, tmp_path: Path) -> None:
        """A second run of the backfill must report rows_updated=0 because
        the WHERE clause only picks rows where turn_count IS NULL."""
        db_path, store = _make_test_db(tmp_path)

        with store._connect() as conn:
            conn.execute(
                "INSERT INTO ticket_metrics "
                "(ticket_id, project_id, epic_id, implementation_mode, outcome) "
                "VALUES (?, ?, ?, ?, ?)",
                ("WOR-900", "proj-test", "WOR-900", "local", "success"),
            )

        # Create a valid log so the first run backfills.
        log_dir = tmp_path / ".claude"
        log_path = log_dir / "worker_wor-900.log"
        _write_worker_log(
            log_path,
            [_assistant_turn(input_tokens=500)],
        )

        # Run backfill once — should populate.
        with patch.object(MetricsStore, "get_db_path", return_value=db_path):
            with patch(
                "scripts.backfill_behavior_telemetry.Path.cwd", return_value=tmp_path
            ):
                main([])

        # Verify populated.
        result = store.get_by_ticket("WOR-900", "proj-test")
        assert result is not None
        assert result.turn_count == 1

        # Run again — turn_count is no longer NULL so nothing is picked up.
        with patch.object(MetricsStore, "get_db_path", return_value=db_path):
            with patch(
                "scripts.backfill_behavior_telemetry.Path.cwd", return_value=tmp_path
            ):
                main([])

        # Row should be unchanged (still turn_count=1).
        result = store.get_by_ticket("WOR-900", "proj-test")
        assert result is not None
        assert result.turn_count == 1

    def test_missing_log_keeps_row_null(self, tmp_path: Path) -> None:
        """Rows whose artifact log is missing on disk are left as NULL.
        The script does not invent data."""
        db_path, store = _make_test_db(tmp_path)

        with store._connect() as conn:
            conn.execute(
                "INSERT INTO ticket_metrics "
                "(ticket_id, project_id, epic_id, implementation_mode, outcome) "
                "VALUES (?, ?, ?, ?, ?)",
                ("WOR-901", "proj-test", "WOR-900", "local", "success"),
            )

        # No log file is created — the path simply does not exist.

        with patch.object(MetricsStore, "get_db_path", return_value=db_path):
            with patch(
                "scripts.backfill_behavior_telemetry.Path.cwd", return_value=tmp_path
            ):
                main([])

        # turn_count should still be NULL.
        result = store.get_by_ticket("WOR-901", "proj-test")
        assert result is not None
        assert result.turn_count is None


class TestBackfillReports:
    """The backfill script reports (rows_total, rows_updated,
    rows_logfile_missing, rows_parse_failed) to stdout."""

    def test_report_breakdown(self, tmp_path: Path) -> None:
        """Multiple rows — some parseable, some missing logs — produce
        correct report counts."""
        db_path, store = _make_test_db(tmp_path)

        with store._connect() as conn:
            conn.execute(
                "INSERT INTO ticket_metrics "
                "(ticket_id, project_id, epic_id, implementation_mode, outcome) "
                "VALUES (?, ?, ?, ?, ?)",
                ("WOR-900", "proj-test", "WOR-900", "local", "success"),
            )
            conn.execute(
                "INSERT INTO ticket_metrics "
                "(ticket_id, project_id, epic_id, implementation_mode, outcome) "
                "VALUES (?, ?, ?, ?, ?)",
                ("WOR-901", "proj-test", "WOR-900", "local", "success"),
            )
            conn.execute(
                "INSERT INTO ticket_metrics "
                "(ticket_id, project_id, epic_id, implementation_mode, outcome) "
                "VALUES (?, ?, ?, ?, ?)",
                ("WOR-902", "proj-test", "WOR-900", "local", "success"),
            )
            # Already-populated row — excluded by WHERE.
            conn.execute(
                "INSERT INTO ticket_metrics "
                "(ticket_id, project_id, epic_id, "
                "implementation_mode, outcome, turn_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("WOR-903", "proj-test", "WOR-900", "local", "success", 5),
            )

        # Create log for WOR-900 only.
        log_dir = tmp_path / ".claude"
        _write_worker_log(
            log_dir / "worker_wor-900.log",
            [_assistant_turn(input_tokens=100)],
        )

        captured: list[str] = []

        class _Capture:
            def write(self, s: str) -> None:  # noqa: D102
                captured.append(s)

            def flush(self) -> None:  # noqa: D102
                pass

        io_override = _Capture()

        with (
            patch.object(MetricsStore, "get_db_path", return_value=db_path),
            patch(
                "scripts.backfill_behavior_telemetry.Path.cwd", return_value=tmp_path
            ),
            patch("scripts.backfill_behavior_telemetry.sys.stdout", io_override),
        ):
            main([])

        report = "".join(captured).strip()
        # parseable=1, missing=2, excluded=1
        assert "rows_total=3" in report
        assert "rows_updated=1" in report
        assert "rows_logfile_missing=2" in report
        assert "rows_parse_failed=0" in report
