"""Tests for app.cli.operator — watcher-softstop, waste-score, ticket-status helpers.

WOR-432: coverage fill for cli/ Phase 3 work. Covers the previously-untested
paths in operator.py (lines 50-83 softstop, 111-134 waste-score, 155-213 the
ticket-status full-output + watch-loop helpers).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from app.cli.operator import (
    _emit_ticket_status_full,
    _run_ticket_status_watch_loop,
    _run_waste_score,
    _run_watcher_softstop,
)
from app.core.watcher.ticket_status import (
    ArtifactInfo,
    LogInfo,
    TicketStatus,
    ToolCallInfo,
)

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture as CapSys
else:
    CapSys = pytest.CaptureFixture  # type: ignore[misc,assignment]


# ── _run_watcher_softstop ──────────────────────────────────────────────────────


def test_softstop_no_daemon_returns_error(tmp_path: Path, capsys: CapSys) -> None:
    """When no .claude/watcher.pid exists, softstop is a no-op + returns 1."""
    with patch("app.cli.operator.Path.cwd", return_value=tmp_path):
        rc = _run_watcher_softstop(argparse.Namespace())
    assert rc == 1
    err = capsys.readouterr().err
    assert "no .claude/watcher.pid" in err


def test_softstop_writes_sentinel(tmp_path: Path, capsys: CapSys) -> None:
    """When daemon is running (pid file exists), softstop writes the sentinel."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    pid_file = claude_dir / "watcher.pid"
    pid_file.write_text("12345", encoding="utf-8")
    with patch("app.cli.operator.Path.cwd", return_value=tmp_path):
        rc = _run_watcher_softstop(argparse.Namespace())
    assert rc == 0
    sentinel = claude_dir / "watcher.softstop"
    assert sentinel.exists()
    assert "PID 12345" in capsys.readouterr().err


def test_softstop_pid_file_unreadable_shows_unknown(
    tmp_path: Path, capsys: CapSys
) -> None:
    """Unreadable pid file shows 'unknown' PID but still writes sentinel."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    pid_file = claude_dir / "watcher.pid"
    pid_file.write_text("99999", encoding="utf-8")
    with (
        patch("app.cli.operator.Path.cwd", return_value=tmp_path),
        patch.object(Path, "read_text", side_effect=OSError("denied")),
    ):
        rc = _run_watcher_softstop(argparse.Namespace())
    assert rc == 0
    assert "PID unknown" in capsys.readouterr().err


# ── _run_waste_score ──────────────────────────────────────────────────────────


def test_waste_score_missing_log_returns_error(
    tmp_path: Path, capsys: CapSys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When .claude/worker_<id>.log doesn't exist, return 1 with a clear error."""
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(ticket_id="WOR-999")
    rc = _run_waste_score(args)
    assert rc == 1
    assert "worker log not found" in capsys.readouterr().err


def test_waste_score_reads_log_and_prints_report(
    tmp_path: Path, capsys: CapSys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When log exists, _run_waste_score calls compute_waste_score and prints report."""
    monkeypatch.chdir(tmp_path)
    claude = tmp_path / ".claude"
    claude.mkdir()
    log = claude / "worker_wor-10.log"
    log.write_text('{"type":"assistant"}\n', encoding="utf-8")

    fake_report = MagicMock(score=42, breakdown={"redundant_reads": 10, "tool_gap": 5})
    with patch(
        "app.core.watcher.worker_waste.compute_waste_score",
        return_value=fake_report,
    ):
        rc = _run_waste_score(argparse.Namespace(ticket_id="WOR-10"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "WOR-10" in out
    assert "42/100" in out
    assert "redundant_reads: 10" in out


# ── _emit_ticket_status_full ──────────────────────────────────────────────────


def _make_status(**overrides) -> TicketStatus:
    """Minimal TicketStatus for tests; overrides fill specific fields."""
    base = {
        "ticket_id": "WOR-10",
        "title": "Test",
        "state": "InProgressLocal",
        "state_age_seconds": 60,
        "worker_log": None,
        "artifacts": None,
        "worktree_exists": None,
        "worktree_path": None,
        "health_flags": {},
    }
    base.update(overrides)
    return TicketStatus(**base)  # type: ignore[arg-type]


def test_emit_full_minimal_status(capsys: CapSys) -> None:
    """A status with all-None optional fields still prints sensibly."""
    _emit_ticket_status_full(_make_status())
    out = capsys.readouterr().out
    assert "Ticket: WOR-10" in out
    assert "no log file found" in out
    assert "Artifacts: none" in out


def test_emit_full_with_worker_log(capsys: CapSys) -> None:
    """Worker log info is rendered when present, including recent actions."""
    log = LogInfo(
        size_bytes=1024,
        last_activity_ago_seconds=30,
        last_tool_calls=[ToolCallInfo(name="Edit", display="foo.py")],
    )
    _emit_ticket_status_full(_make_status(worker_log=log))
    out = capsys.readouterr().out
    assert "Worker process:" in out
    assert "Edit foo.py" in out


def test_emit_full_with_artifacts(capsys: CapSys, tmp_path: Path) -> None:
    """Artifacts entries are listed line-by-line."""
    art = ArtifactInfo(
        path=str(tmp_path / ".claude/artifacts/wor_10"),
        entries={"manifest.json": 1200},
    )
    _emit_ticket_status_full(_make_status(artifacts=art))
    out = capsys.readouterr().out
    assert "Artifacts" in out
    assert "manifest.json" in out


def test_emit_full_with_worktree_exists(capsys: CapSys, tmp_path: Path) -> None:
    """worktree_exists=True renders the worktree path."""
    _emit_ticket_status_full(
        _make_status(worktree_exists=True, worktree_path=tmp_path / "wt")
    )
    out = capsys.readouterr().out
    assert "Worktree" in out
    assert "exists" in out


def test_emit_full_with_worktree_missing(capsys: CapSys) -> None:
    """worktree_exists=False renders 'none'."""
    _emit_ticket_status_full(_make_status(worktree_exists=False))
    assert "Worktree: none" in capsys.readouterr().out


def test_emit_full_with_health_flags(capsys: CapSys) -> None:
    """Health flag entries render as a single line."""
    flags = {"api_retries": 3, "subagent_spawns": 2, "no_result_artifact": True}
    _emit_ticket_status_full(_make_status(health_flags=flags))
    out = capsys.readouterr().out
    assert "Health flags" in out
    assert "3 api_retry events" in out
    assert "2 subagent spawns" in out
    assert "no result artifact yet" in out


# ── _run_ticket_status_watch_loop ─────────────────────────────────────────────


def test_watch_loop_exits_on_terminal_state(capsys: CapSys) -> None:
    """If status is already in a terminal state, loop exits immediately (no poll)."""
    status = _make_status(state="Done")
    client = MagicMock()
    rc = _run_ticket_status_watch_loop(client, "WOR-10", status)
    assert rc is None
    assert "terminal state: Done" in capsys.readouterr().out
    client.assert_not_called()


def test_watch_loop_polls_then_exits_on_state_change(capsys: CapSys) -> None:
    """Non-terminal initial state polls once, transitions to terminal, exits."""
    initial = _make_status(state="InProgressLocal")
    next_status = _make_status(state="MergedToEpic")
    client = MagicMock()
    with (
        patch("app.cli.operator.time.sleep"),
        patch(
            "app.cli.operator.fetch_ticket_status",
            side_effect=[next_status],
        ),
    ):
        rc = _run_ticket_status_watch_loop(client, "WOR-10", initial)
    assert rc is None
    out = capsys.readouterr().out
    assert "polled" in out
    assert "terminal state: MergedToEpic" in out


def test_watch_loop_keyboard_interrupt_returns_zero(capsys: CapSys) -> None:
    """KeyboardInterrupt during sleep cleanly returns 0."""
    status = _make_status(state="InProgressLocal")
    client = MagicMock()
    with patch("app.cli.operator.time.sleep", side_effect=KeyboardInterrupt):
        rc = _run_ticket_status_watch_loop(client, "WOR-10", status)
    assert rc is None
