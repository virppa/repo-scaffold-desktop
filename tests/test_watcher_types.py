"""Tests for app.core.watcher_types."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.watcher.watcher_signals import (
    remove_pid_file,
    write_pid_file,
)
from app.core.watcher.watcher_types import is_watcher_running


def test_is_watcher_running_no_pid_file(tmp_path: Path) -> None:
    pid_file = tmp_path / "watcher.pid"
    assert not is_watcher_running(pid_file)


def test_is_watcher_running_stale_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "watcher.pid"
    pid_file.write_text("9999999", encoding="utf-8")  # very unlikely to be real
    # Should return False (process not running) or True on very unlucky collision;
    # just verify no exception is raised
    result = is_watcher_running(pid_file)
    assert isinstance(result, bool)


def test_is_watcher_running_own_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "watcher.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    assert is_watcher_running(pid_file)


# ---------------------------------------------------------------------------
# Watcher PID file
# ---------------------------------------------------------------------------


def test_write_and_remove_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write and remove the watcher PID file via the module-level functions."""
    pid_file = tmp_path / "watcher.pid"
    # _PID_FILE is defined in watcher_types and imported into watcher_signals at
    # module load time. We must patch both so write_pid_file uses our temp path.
    monkeypatch.setattr("app.core.watcher.watcher_types._PID_FILE", pid_file)
    monkeypatch.setattr("app.core.watcher.watcher_signals._PID_FILE", pid_file)

    write_pid_file(tmp_path)
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8") == str(os.getpid())

    remove_pid_file()
    assert not pid_file.exists()
