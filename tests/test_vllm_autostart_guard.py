"""WOR-492: the autouse conftest guards prevent any real WSL/vLLM contact.

Regression guards for the leak where a full pytest run on Windows (a) spawned
real `vllm serve` GPU processes via ServiceManager's win32 auto-start and
(b) hammered an already-running vLLM with real GET /v1/models, GET /metrics
and POST /v1/messages (a real inference) calls.
"""

from __future__ import annotations

import http.client
import subprocess  # noqa: S404 — guard assertions only
from pathlib import Path

import pytest

from app.core.watcher.watcher_services import (
    _VLLM_HOST,
    _VLLM_PORT,
    ServiceManager,
)


def test_guard_blocks_wt_exe_spawn() -> None:
    """The win32 vLLM auto-start (`wt.exe`) is refused with the same
    FileNotFoundError `_open_vllm_terminal` already handles gracefully."""
    with pytest.raises(FileNotFoundError, match="WOR-492"):
        subprocess.Popen(["wt.exe", "-w", "0", "new-tab", "--", "wsl"])


def test_guard_refuses_real_vllm_http_connection() -> None:
    """A real HTTPConnection to the vLLM host:port is refused so no test
    can hit a running vLLM by accident."""
    with pytest.raises(OSError, match="WOR-492"):
        http.client.HTTPConnection(_VLLM_HOST, _VLLM_PORT, timeout=3)


def test_non_vllm_http_connection_still_constructs() -> None:
    """The HTTP guard is scoped to the vLLM host:port only — other hosts
    are untouched (constructing the object does not open a socket)."""
    conn = http.client.HTTPConnection("example.invalid", 1234, timeout=1)
    assert conn.host == "example.invalid"


def test_probe_vllm_health_unmocked_fails_closed(tmp_path: Path) -> None:
    """With NO local mocks, probe_vllm_health exercises the full path
    (HTTP refused by the guard, then the win32 terminal branch whose
    wt.exe Popen is refused) and returns False — no real network call,
    no real process spawn."""
    mgr = ServiceManager(tmp_path)
    assert mgr.probe_vllm_health() is False
