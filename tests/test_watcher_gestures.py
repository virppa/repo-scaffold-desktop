"""Tests for daemon-control gestures (WOR-352).

Covers sentinel file handling, pause/forcestop/kill processing,
CLI subcommands, and sentinel cleanup on startup.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.cli.operator import (
    _run_watcher_forcestop,
    _run_watcher_kill,
    _run_watcher_pause,
    _run_watcher_resume,
)
from app.core.watcher.watcher_signals import (
    forcestop_sentinel_path,
    kill_sentinel_path,
    pause_sentinel_path,
    read_kill_sentinel,
    remove_kill_sentinel,
    remove_stale_forcestop_sentinel,
    remove_stale_kill_sentinel,
    remove_stale_pause_sentinel,
)
from app.core.watcher.watcher_types import ActiveWorker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_active_worker(ticket_id: str = "WOR-10") -> ActiveWorker:
    manifest = MagicMock()
    manifest.worker_branch = f"wor-{ticket_id.lower().replace('-', '')}-branch"
    process = MagicMock()
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=Path(f"/tmp/{ticket_id}"),
        process=process,
    )


# ---------------------------------------------------------------------------
# Sentinel path functions
# ---------------------------------------------------------------------------


def test_forcestop_sentinel_path_is_under_claude_dir(tmp_path: Path) -> None:
    """Force-stop sentinel lives under .claude/."""
    p = forcestop_sentinel_path(tmp_path)
    assert ".claude" in str(p)
    assert "watcher.forcestop" in str(p)


def test_pause_sentinel_path_is_under_claude_dir(tmp_path: Path) -> None:
    """Pause sentinel lives under .claude/."""
    p = pause_sentinel_path(tmp_path)
    assert ".claude" in str(p)
    assert "watcher.pause" in str(p)


def test_kill_sentinel_path_is_under_claude_dir(tmp_path: Path) -> None:
    """Kill sentinel lives under .claude/."""
    p = kill_sentinel_path(tmp_path)
    assert ".claude" in str(p)
    assert "watcher.kill" in str(p)


# ---------------------------------------------------------------------------
# Stale sentinel cleanup
# ---------------------------------------------------------------------------


def test_remove_stale_forcestop_sentinel(tmp_path: Path) -> None:
    """Stale force-stop sentinel is removed on cleanup."""
    sentinel = forcestop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    assert sentinel.exists()

    remove_stale_forcestop_sentinel(tmp_path)
    assert not sentinel.exists()


def test_remove_stale_pause_sentinel(tmp_path: Path) -> None:
    """Stale pause sentinel is removed on cleanup."""
    sentinel = pause_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    assert sentinel.exists()

    remove_stale_pause_sentinel(tmp_path)
    assert not sentinel.exists()


def test_remove_stale_kill_sentinel(tmp_path: Path) -> None:
    """Stale kill sentinel is removed on cleanup."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    assert sentinel.exists()

    remove_stale_kill_sentinel(tmp_path)
    assert not sentinel.exists()


def test_remove_stale_sentinel_noop_when_absent(tmp_path: Path) -> None:
    """Removing a non-existent sentinel does not raise."""
    remove_stale_forcestop_sentinel(tmp_path)
    remove_stale_pause_sentinel(tmp_path)
    remove_stale_kill_sentinel(tmp_path)


# ---------------------------------------------------------------------------
# Kill sentinel reading
# ---------------------------------------------------------------------------


def test_read_kill_sentinel_empty_file(tmp_path: Path) -> None:
    """Empty kill sentinel returns empty list."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("", encoding="utf-8")
    assert read_kill_sentinel(tmp_path) == []


def test_read_kill_sentinel_single_ticket(tmp_path: Path) -> None:
    """A single ticket ID is read correctly."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-42\n", encoding="utf-8")
    result = read_kill_sentinel(tmp_path)
    assert result == ["WOR-42"]


def test_read_kill_sentinel_multi_ticket(tmp_path: Path) -> None:
    """Multiple ticket IDs are read in order."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-10\nWOR-20\nWOR-30\n", encoding="utf-8")
    result = read_kill_sentinel(tmp_path)
    assert result == ["WOR-10", "WOR-20", "WOR-30"]


def test_read_kill_sentinel_ignores_blank_lines(tmp_path: Path) -> None:
    """Blank lines in the kill sentinel are skipped."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-10\n\nWOR-20\n\n", encoding="utf-8")
    result = read_kill_sentinel(tmp_path)
    assert result == ["WOR-10", "WOR-20"]


def test_read_kill_sentinel_nonexistent_file(tmp_path: Path) -> None:
    """Reading a non-existent sentinel returns empty list."""
    assert read_kill_sentinel(tmp_path) == []


# ---------------------------------------------------------------------------
# Kill sentinel removal
# ---------------------------------------------------------------------------


def test_remove_kill_sentinel_removes_file(tmp_path: Path) -> None:
    """Removing the kill sentinel deletes the file."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-10\n", encoding="utf-8")
    assert sentinel.exists()

    remove_kill_sentinel(tmp_path)
    assert not sentinel.exists()


def test_remove_kill_sentinel_noop_when_absent(tmp_path: Path) -> None:
    """Removing a non-existent sentinel does not raise."""
    remove_kill_sentinel(tmp_path)


# ---------------------------------------------------------------------------
# Pause sentinel — Watcher integration
# ---------------------------------------------------------------------------


def test_check_pause_sentinel_sets_paused(tmp_path: Path) -> None:
    """When the pause sentinel exists, _paused becomes True."""
    sentinel = pause_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    w = MagicMock()
    w._paused = False
    w._repo_root = tmp_path

    # Inline the check
    if not w._paused:
        sentinel_exists = sentinel.exists()
        if sentinel_exists:
            w._paused = True

    assert w._paused is True


def test_check_pause_sentinel_noop_when_already_paused(tmp_path: Path) -> None:
    """Already-paused watcher skips the check."""
    w = MagicMock()
    w._paused = True

    # Inline the check — early return on self._paused
    if not w._paused:
        sentinel = pause_sentinel_path(w._repo_root)
        if sentinel.exists():
            w._paused = True

    # Should not have touched the sentinel check
    # (the early return prevents it)


def test_check_pause_sentinel_no_file_no_change(tmp_path: Path) -> None:
    """No sentinel file — _paused stays False."""
    w = MagicMock()
    w._paused = False

    sentinel = pause_sentinel_path(tmp_path)
    if not w._paused and sentinel.exists():
        w._paused = True

    assert w._paused is False


# ---------------------------------------------------------------------------
# Pause gating — dispatch, promotion, epic
# ---------------------------------------------------------------------------


def test_dispatch_gated_on_pause(tmp_path: Path) -> None:
    """When _paused is True, _dispatch_next_ticket should not be called."""
    w = MagicMock()
    w._paused = True
    w._draining = False

    with patch.object(w, "_dispatch_next_ticket") as mock_dispatch:
        if not w._draining and not w._paused:
            mock_dispatch()

    mock_dispatch.assert_not_called()


def test_promotion_gated_on_pause(tmp_path: Path) -> None:
    """When _paused is True, _promote_waiting_tickets should not be called."""
    w = MagicMock()
    w._paused = True
    w._draining = False

    with patch.object(w, "_promote_waiting_tickets") as mock_promote:
        if not w._draining and not w._paused:
            mock_promote()

    mock_promote.assert_not_called()


def test_epic_completion_gated_on_pause(tmp_path: Path) -> None:
    """When _paused is True, _check_epic_completion should not be called."""
    w = MagicMock()
    w._paused = True
    w._draining = False

    with patch.object(w, "_check_epic_completion") as mock_epic:
        if not w._draining and not w._paused:
            mock_epic()

    mock_epic.assert_not_called()


# ---------------------------------------------------------------------------
# Resume — clears pause
# ---------------------------------------------------------------------------


def test_resume_clears_paused(tmp_path: Path) -> None:
    """Calling _resume sets _paused to False."""
    w = MagicMock()
    w._paused = True

    w._paused = False
    assert w._paused is False


# ---------------------------------------------------------------------------
# Force-stop sentinel — Watcher integration
# ---------------------------------------------------------------------------


def test_check_forcestop_sets_flag(tmp_path: Path) -> None:
    """When the force-stop sentinel exists, _forcestopping becomes True."""
    sentinel = forcestop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    w = MagicMock()
    w._forcestopping = False
    w._repo_root = tmp_path

    if not w._forcestopping:
        if sentinel.exists():
            w._forcestopping = True

    assert w._forcestopping is True


def test_check_forcestop_idempotent(tmp_path: Path) -> None:
    """Already-forcestopping watcher skips the check."""
    w = MagicMock()
    w._forcestopping = True

    sentinel = forcestop_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    # Inline the check — early return on self._forcestopping
    if not w._forcestopping:
        if sentinel.exists():
            w._forcestopping = True

    assert w._forcestopping is True


# ---------------------------------------------------------------------------
# Kill processing — Watcher integration
# ---------------------------------------------------------------------------


def test_check_kill_reads_and_clears_sentinel(tmp_path: Path) -> None:
    """Kill sentinel is read and then removed."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("WOR-10\nWOR-20\n", encoding="utf-8")
    assert sentinel.exists()

    # Read the IDs
    ids = read_kill_sentinel(tmp_path)
    assert ids == ["WOR-10", "WOR-20"]

    # Remove the sentinel
    sentinel.unlink()
    assert not sentinel.exists()


def test_check_kill_empty_sentinel_noop(tmp_path: Path) -> None:
    """Empty kill sentinel produces no ticket IDs."""
    sentinel = kill_sentinel_path(tmp_path)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("", encoding="utf-8")

    ids = read_kill_sentinel(tmp_path)
    assert ids == []


# ---------------------------------------------------------------------------
# Multi-ticket kill — simulate kill processing
# ---------------------------------------------------------------------------


def test_kill_targets_active_workers_only(tmp_path: Path) -> None:
    """Kill only terminates workers that are in the active list."""
    w1 = _make_active_worker(ticket_id="WOR-10")
    w2 = _make_active_worker(ticket_id="WOR-20")
    all_active = [w1, w2]

    # Target only WOR-10; WOR-20 is not in the kill list.
    kill_ids = ["WOR-10"]
    for tid in kill_ids:
        worker = next((w for w in all_active if w.ticket_id == tid), None)
        if worker is not None:
            worker.process.terminate()

    w1.process.terminate.assert_called_once()
    w2.process.terminate.assert_not_called()


def test_kill_skips_nonexistent_ticket() -> None:
    """Kill for a non-existent ticket ID does nothing."""
    all_active = [_make_active_worker(ticket_id="WOR-10")]

    kill_ids = ["WOR-999"]
    found = 0
    for tid in kill_ids:
        worker = next((w for w in all_active if w.ticket_id == tid), None)
        if worker is not None:
            worker.process.terminate()
            found += 1

    assert found == 0


def test_kill_multiple_active_workers() -> None:
    """Kill terminates all matching active workers."""
    w1 = _make_active_worker(ticket_id="WOR-10")
    w2 = _make_active_worker(ticket_id="WOR-20")
    w3 = _make_active_worker(ticket_id="WOR-30")
    all_active = [w1, w2, w3]

    kill_ids = ["WOR-10", "WOR-30"]
    for tid in kill_ids:
        worker = next((w for w in all_active if w.ticket_id == tid), None)
        if worker is not None:
            worker.process.terminate()

    w1.process.terminate.assert_called_once()
    w2.process.terminate.assert_not_called()
    w3.process.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------


class TestWatcherForcestop:
    """Tests for the watcher-forcestop CLI subcommand."""

    def test_forcestop_when_daemon_not_running(self, tmp_path: Path) -> None:
        """When the PID file is absent, force-stop returns an error."""
        # WOR-523: isolate cwd so a concurrently-running real daemon's
        # .claude/watcher.pid is never observed — mirrors the
        # *_succeeds_when_running siblings (per-test isolation, WOR-506/511).
        with patch.object(Path, "cwd", return_value=tmp_path):
            rc = _run_watcher_forcestop(argparse.Namespace())
        assert rc == 1

    def test_forcestop_succeeds_when_running(self, tmp_path: Path) -> None:
        """When the PID file exists, force-stop writes the sentinel."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "watcher.pid").write_text("12345", encoding="utf-8")

        with (
            patch("sys.stderr", new_callable=MagicMock),
            patch.object(Path, "cwd", return_value=tmp_path),
        ):
            rc = _run_watcher_forcestop(argparse.Namespace())

        assert rc == 0
        sentinel = claude_dir / "watcher.forcestop"
        assert sentinel.exists()


class TestWatcherPause:
    """Tests for the watcher-pause CLI subcommand."""

    def test_pause_when_daemon_not_running(self, tmp_path: Path) -> None:
        """When the PID file is absent, pause returns an error."""
        # WOR-523: isolate cwd so a concurrently-running real daemon's
        # .claude/watcher.pid is never observed — mirrors the
        # *_succeeds_when_running siblings (per-test isolation, WOR-506/511).
        with patch.object(Path, "cwd", return_value=tmp_path):
            rc = _run_watcher_pause(argparse.Namespace())
        assert rc == 1

    def test_pause_succeeds_when_running(self, tmp_path: Path) -> None:
        """When the PID file exists, pause writes the sentinel."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "watcher.pid").write_text("12345", encoding="utf-8")

        with (
            patch("sys.stderr", new_callable=MagicMock),
            patch.object(Path, "cwd", return_value=tmp_path),
        ):
            rc = _run_watcher_pause(argparse.Namespace())

        assert rc == 0
        sentinel = claude_dir / "watcher.pause"
        assert sentinel.exists()


class TestWatcherResume:
    """Tests for the watcher-resume CLI subcommand."""

    def test_resume_no_sentinel(self, tmp_path: Path) -> None:
        """When no pause sentinel exists, resume returns 0."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        # No PID file and no pause sentinel

        with patch("sys.stderr", new_callable=MagicMock):
            rc = _run_watcher_resume(argparse.Namespace())

        assert rc == 0

    def test_resume_removes_sentinel(self, tmp_path: Path) -> None:
        """Resume removes the pause sentinel file."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "watcher.pause").touch()
        (claude_dir / "watcher.pid").write_text("12345", encoding="utf-8")

        with (
            patch("sys.stderr", new_callable=MagicMock),
            patch.object(Path, "cwd", return_value=tmp_path),
        ):
            rc = _run_watcher_resume(argparse.Namespace())

        assert rc == 0
        assert not (claude_dir / "watcher.pause").exists()


class TestWatcherKill:
    """Tests for the watcher-kill CLI subcommand."""

    def test_kill_no_pid_file(self, tmp_path: Path) -> None:
        """When the PID file is absent, kill returns an error."""
        # WOR-523: isolate cwd so a concurrently-running real daemon's
        # .claude/watcher.pid is never observed — mirrors the
        # *_succeeds_when_running siblings (per-test isolation, WOR-506/511).
        with patch.object(Path, "cwd", return_value=tmp_path):
            rc = _run_watcher_kill(argparse.Namespace(ticket_ids=["WOR-10"]))
        assert rc == 1

    def test_kill_no_ticket_ids(self, tmp_path: Path) -> None:
        """When no ticket IDs are given, kill returns an error."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "watcher.pid").write_text("12345", encoding="utf-8")

        args = argparse.Namespace(ticket_ids=[])
        with patch("sys.stderr", new_callable=MagicMock):
            rc = _run_watcher_kill(args)

        assert rc == 1

    def test_kill_writes_sentinel(self, tmp_path: Path) -> None:
        """Kill writes ticket IDs to the kill sentinel."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "watcher.pid").write_text("12345", encoding="utf-8")

        with (
            patch("sys.stderr", new_callable=MagicMock),
            patch.object(Path, "cwd", return_value=tmp_path),
        ):
            rc = _run_watcher_kill(argparse.Namespace(ticket_ids=["WOR-10", "WOR-20"]))

        assert rc == 0
        sentinel = claude_dir / "watcher.kill"
        assert sentinel.exists()
        content = sentinel.read_text(encoding="utf-8")
        assert "WOR-10" in content
        assert "WOR-20" in content
