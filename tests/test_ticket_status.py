"""Tests for the pure ticket-status assembler (app/core/watcher/ticket_status.py)."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.watcher.ticket_status import (
    _format_age,
    _format_size,
    _parse_last_tool_calls,
    fetch_ticket_status,
)

# ── Helper tests ───────────────────────────────────────────────────────────────


class TestFormatAge:
    def test_zero_seconds(self) -> None:
        assert _format_age(0) == "0s"

    def test_seconds(self) -> None:
        assert _format_age(45) == "45s"

    def test_minutes(self) -> None:
        assert _format_age(120) == "2m"

    def test_minutes_with_remainder(self) -> None:
        assert _format_age(125) == "2m05s"

    def test_hours(self) -> None:
        assert _format_age(7200) == "2h"

    def test_hours_with_minutes(self) -> None:
        assert _format_age(7500) == "2h05m"

    def test_none(self) -> None:
        assert _format_age(None) == "?"


class TestFormatSize:
    def test_bytes(self) -> None:
        assert _format_size(500) == "500B"

    def test_kilobytes_exact(self) -> None:
        assert _format_size(2048) == "2KB"

    def test_kilobytes_fractional(self) -> None:
        assert _format_size(2560) == "2.5KB"

    def test_megabytes(self) -> None:
        assert _format_size(1_048_576) == "1.0MB"

    def test_none(self) -> None:
        assert _format_size(None) == "?"


# ── Log parsing tests ──────────────────────────────────────────────────────────


class TestParseLastToolCalls:
    def test_empty_log(self, tmp_path: Path) -> None:
        log_path = tmp_path / "worker_wor_99.log"
        log_path.write_text("", encoding="utf-8")
        result = _parse_last_tool_calls(log_path)
        assert result == []

    def test_no_assistant_events(self, tmp_path: Path) -> None:
        log_path = tmp_path / "worker_wor_99.log"
        system_event = '{"type":"system","subtype":"api_retry"}\n'
        log_path.write_text(system_event, encoding="utf-8")
        result = _parse_last_tool_calls(log_path)
        assert result == []

    def test_extracts_single_tool_call(self, tmp_path: Path) -> None:
        log_path = tmp_path / "worker_wor_99.log"
        path_arg = "app/core/X.py"
        payload = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "path": path_arg,
                                "old_string": "a",
                                "new_string": "b",
                            },
                        }
                    ]
                },
            }
        )
        log_path.write_text(payload + "\n", encoding="utf-8")
        result = _parse_last_tool_calls(log_path)
        assert len(result) == 1
        assert result[0].name == "Edit"
        assert "app/core/X.py" in result[0].display

    def test_truncates_long_display(self, tmp_path: Path) -> None:
        log_path = tmp_path / "worker_wor_99.log"
        payload = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Task",
                            "input": {"prompt": "A" * 100},
                        }
                    ]
                },
            }
        )
        log_path.write_text(payload + "\n", encoding="utf-8")
        result = _parse_last_tool_calls(log_path)
        assert len(result) == 1
        assert len(result[0].display) <= 40

    def test_returns_max_three(self, tmp_path: Path) -> None:
        log_path = tmp_path / "worker_wor_99.log"
        entries: list[str] = []
        for i in range(5):
            entries.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"path": f"app/f{i}.py"},
                                }
                            ]
                        },
                    }
                )
            )
        log_path.write_text("\n".join(entries) + "\n", encoding="utf-8")
        result = _parse_last_tool_calls(log_path)
        assert len(result) == 3


# ── fetch_ticket_status tests ──────────────────────────────────────────────────


def _make_mock_issue(
    state_name: str = "InProgressLocal", title: str = "Test ticket"
) -> MagicMock:
    """Create a mock LinearClient that returns a valid issue dict."""
    client = MagicMock()
    client.get_issue.return_value = {
        "title": title,
        "state": {"name": state_name, "createdAt": "2026-05-10T06:19:32.000Z"},
    }
    return client


def test_in_flight_ticket(tmp_path: Path) -> None:
    """An in-flight ticket should show log, artifacts, and worktree info."""
    client = _make_mock_issue("InProgressLocal", "In-progress ticket")

    # Create a dummy log file
    log_dir = tmp_path / ".claude" / "artifacts" / "wor_inprogress"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "worker_wor_inprogress.log"
    log_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"path": "app/x.py"},
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Create a dummy artifact
    (log_dir / "manifest.json").write_text('{"ticket":"WOR-123"}', encoding="utf-8")

    # Create dummy worktree
    (tmp_path / ".claude" / "worktrees" / "wor_inprogress").mkdir(parents=True)

    with patch(
        "app.core.watcher.ticket_status._parse_log_path",
        return_value=log_path,
    ):
        status = fetch_ticket_status(
            client,
            "WOR-INPROGRESS",
            artifact_dir=log_dir,
            worktree_dir=tmp_path / ".claude" / "worktrees" / "wor_inprogress",
        )

    assert status.ticket_id == "WOR-INPROGRESS"
    assert status.title == "In-progress ticket"
    assert status.state == "InProgressLocal"
    assert status.worker_log is not None
    assert status.worker_log.size_bytes > 0
    assert status.worker_log.last_tool_calls
    assert status.artifacts is not None
    assert "manifest.json" in status.artifacts.entries
    assert status.worktree_exists is True
    assert "no_result_artifact" in status.health_flags


def test_terminal_ticket(tmp_path: Path) -> None:
    """A done ticket should not flag missing result artifact."""
    client = _make_mock_issue("Done")

    status = fetch_ticket_status(
        client, "WOR-123", artifact_dir=tmp_path / "nonexistent"
    )

    assert status.state == "Done"
    assert "no_result_artifact" not in status.health_flags


def test_linear_error(tmp_path: Path) -> None:
    """When Linear is unreachable, return a minimal status."""
    client = MagicMock()
    client.get_issue.side_effect = Exception("network unreachable")

    status = fetch_ticket_status(client, "WOR-123")

    assert status.ticket_id == "WOR-123"
    assert "network unreachable" in status.title
    assert status.state == "Unknown"
    assert status.worker_log is None
    assert status.worktree_exists is None


def test_to_dict_serialization() -> None:
    """TicketStatus.to_dict should produce a JSON-serialisable dict."""
    status = fetch_ticket_status(_make_mock_issue("Done"), "WOR-123")
    data: dict[str, Any] = status.to_dict()
    # Should round-trip through json.dumps/dumps
    text = json.dumps(data)
    assert json.loads(text) == data
    assert data["is_terminal"] is True


def test_brief_terminal_state() -> None:
    """Terminal states should make is_terminal=True."""
    for state in ["Done", "MergedToEpic", "Cancelled", "Duplicate", "Blocked"]:
        client = _make_mock_issue(state)
        status = fetch_ticket_status(client, "WOR-123")
        assert status.is_terminal is True, f"Expected is_terminal=True for {state}"
