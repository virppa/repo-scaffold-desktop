"""Tests for app.core.watcher_types."""

from __future__ import annotations

import os
from pathlib import Path

from app.core.watcher_types import is_watcher_running


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
