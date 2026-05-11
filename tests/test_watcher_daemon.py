"""Tests for app.core.watcher.watcher_daemon (WOR-435)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.watcher.watcher_daemon import (
    launch_detached,
    launch_in_new_terminal,
    load_env_file,
)

# ── load_env_file ───────────────────────────────────────────────────────────


def test_load_env_file_missing_returns_zero(tmp_path: Path) -> None:
    """No .env file → returns 0, no env changes."""
    before = dict(os.environ)
    n = load_env_file(repo_root=tmp_path)
    assert n == 0
    assert dict(os.environ) == before


def test_load_env_file_loads_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env with two new keys → both loaded into os.environ."""
    env_path = tmp_path / ".env"
    env_path.write_text("WOR435_TEST_A=alpha\nWOR435_TEST_B=beta\n", encoding="utf-8")
    monkeypatch.delenv("WOR435_TEST_A", raising=False)
    monkeypatch.delenv("WOR435_TEST_B", raising=False)

    n = load_env_file(repo_root=tmp_path)

    assert n == 2
    assert os.environ["WOR435_TEST_A"] == "alpha"
    assert os.environ["WOR435_TEST_B"] == "beta"


def test_load_env_file_existing_env_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys already set in os.environ are NOT overwritten by .env."""
    env_path = tmp_path / ".env"
    env_path.write_text("WOR435_PRECEDENCE=fromfile\n", encoding="utf-8")
    monkeypatch.setenv("WOR435_PRECEDENCE", "fromenv")

    n = load_env_file(repo_root=tmp_path)

    assert n == 0  # already set; not counted as loaded
    assert os.environ["WOR435_PRECEDENCE"] == "fromenv"


def test_load_env_file_unreadable_returns_zero_logs_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """OSError reading .env → returns 0, warning logged, no env changes."""
    env_path = tmp_path / ".env"
    env_path.write_text("WOR435_X=1\n", encoding="utf-8")

    # Shadow dotenv_values at its source module so the inline import picks
    # up the patched function.
    with patch("dotenv.dotenv_values", side_effect=OSError("denied")):
        with caplog.at_level("WARNING"):
            n = load_env_file(repo_root=tmp_path)

    assert n == 0
    assert any("Failed to read" in rec.message for rec in caplog.records)


# ── launch_detached ─────────────────────────────────────────────────────────


def test_launch_detached_returns_child_pid(tmp_path: Path) -> None:
    """Popen is invoked with the watcher command; returns the child PID."""
    fake_proc = MagicMock(pid=99999)
    with patch(
        "app.core.watcher.watcher_daemon.subprocess.Popen", return_value=fake_proc
    ) as mock_popen:
        pid = launch_detached(repo_root=tmp_path)

    assert pid == 99999
    call = mock_popen.call_args
    cmd = call.args[0]
    assert cmd[0] == sys.executable
    assert cmd[1:] == ["-m", "app.cli", "watcher"]
    assert call.kwargs["cwd"] == str(tmp_path)


def test_launch_detached_creates_log_dir_and_file(tmp_path: Path) -> None:
    """`.claude/watcher.log` is created before Popen so the child can inherit the fd."""
    with patch("app.core.watcher.watcher_daemon.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=1)
        launch_detached(repo_root=tmp_path)

    assert (tmp_path / ".claude" / "watcher.log").exists()
    assert (tmp_path / ".claude").is_dir()


def test_launch_detached_platform_specific_flags(tmp_path: Path) -> None:
    """Windows path passes creationflags; POSIX path passes start_new_session=True."""
    with patch("app.core.watcher.watcher_daemon.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=1)
        with patch("app.core.watcher.watcher_daemon.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.executable = sys.executable
            launch_detached(repo_root=tmp_path)
        assert "creationflags" in mock_popen.call_args.kwargs

    with patch("app.core.watcher.watcher_daemon.subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock(pid=1)
        with patch("app.core.watcher.watcher_daemon.sys") as mock_sys:
            mock_sys.platform = "linux"
            mock_sys.executable = sys.executable
            launch_detached(repo_root=tmp_path)
        assert mock_popen.call_args.kwargs.get("start_new_session") is True


# ── launch_in_new_terminal ──────────────────────────────────────────────────


def test_launch_in_new_terminal_non_windows_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-Windows platform → returns 1 with a clear stderr message."""
    monkeypatch.setattr(
        "app.core.watcher.watcher_daemon.sys.platform", "linux", raising=False
    )
    rc = launch_in_new_terminal(repo_root=tmp_path)
    assert rc == 1
    assert "only supported on Windows" in capsys.readouterr().err


def test_launch_in_new_terminal_windows_runs_cmd_start(tmp_path: Path) -> None:
    """Windows path invokes `cmd.exe /c start watcher cmd /k <inner>`."""
    with (
        patch("app.core.watcher.watcher_daemon.sys") as mock_sys,
        patch("app.core.watcher.watcher_daemon.subprocess.run") as mock_run,
    ):
        mock_sys.platform = "win32"
        mock_sys.executable = sys.executable
        rc = launch_in_new_terminal(repo_root=tmp_path)
    assert rc == 0
    args = mock_run.call_args.args[0]
    assert args[0] == "cmd.exe"
    assert "start" in args
    assert "cmd" in args
    # The inner command should include the tmp_path and the watcher invocation
    inner = args[-1]
    assert str(tmp_path) in inner
    assert "app.cli watcher" in inner
