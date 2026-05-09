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


# ---------------------------------------------------------------------------
# WOR-408: WATCHER_VLLM_BASE_URL env override
# ---------------------------------------------------------------------------


def test_vllm_base_url_defaults_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When WATCHER_VLLM_BASE_URL is unset, _VLLM_BASE_URL falls back to
    localhost:8000 — the normal WSL2 port-forwarding case."""
    import importlib

    monkeypatch.delenv("WATCHER_VLLM_BASE_URL", raising=False)
    import app.core.watcher.watcher_types as wt

    importlib.reload(wt)
    assert wt._VLLM_BASE_URL == "http://localhost:8000"


def test_vllm_base_url_honors_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WATCHER_VLLM_BASE_URL fully overrides _VLLM_BASE_URL — used when WSL2
    localhost forwarding is broken and only the WSL guest IP is reachable
    from Windows."""
    import importlib

    monkeypatch.setenv("WATCHER_VLLM_BASE_URL", "http://172.23.139.95:8000")
    import app.core.watcher.watcher_types as wt

    importlib.reload(wt)
    assert wt._VLLM_BASE_URL == "http://172.23.139.95:8000"

    # Reset to default so other tests aren't affected
    monkeypatch.delenv("WATCHER_VLLM_BASE_URL", raising=False)
    importlib.reload(wt)


def test_vllm_host_derives_from_base_url_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_VLLM_HOST defaults to "localhost" when no env override is set."""
    import importlib

    monkeypatch.delenv("WATCHER_VLLM_BASE_URL", raising=False)
    import app.core.watcher.watcher_types as wt

    importlib.reload(wt)
    assert wt._VLLM_HOST == "localhost"


def test_vllm_host_derives_from_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When WATCHER_VLLM_BASE_URL is set, _VLLM_HOST is parsed from it.
    ServiceManager probes use _VLLM_HOST directly (http.client.HTTPConnection
    requires host + port separately, not a URL). Without this propagation, the
    probes would still hit localhost and fail when WSL2 forwarding is broken,
    triggering the spurious "open new vLLM terminal" code path.
    """
    import importlib

    monkeypatch.setenv("WATCHER_VLLM_BASE_URL", "http://172.23.139.95:8000")
    import app.core.watcher.watcher_types as wt

    importlib.reload(wt)
    assert wt._VLLM_HOST == "172.23.139.95"

    monkeypatch.delenv("WATCHER_VLLM_BASE_URL", raising=False)
    importlib.reload(wt)
