"""Tests for app.core.watcher_services (ServiceManager)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.watcher_services import ServiceManager

# ---------------------------------------------------------------------------
# ServiceManager.stop  (formerly _stop_litellm_proxy via Watcher shim)
# ---------------------------------------------------------------------------


def test_stop_terminates_on_clean_exit(tmp_path: Path) -> None:
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 12345
    mock_proc.wait.return_value = 0

    mgr = ServiceManager(tmp_path)
    mgr._litellm_proc = mock_proc

    mgr.stop()

    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_not_called()
    assert mgr._litellm_proc is None


def test_stop_kills_when_terminate_hangs(tmp_path: Path) -> None:
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 12345
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="litellm", timeout=5)

    mgr = ServiceManager(tmp_path)
    mgr._litellm_proc = mock_proc

    mgr.stop()

    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()
    assert mgr._litellm_proc is None


def test_stop_noop_when_no_proc(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    assert mgr._litellm_proc is None
    mgr.stop()  # must not raise


# ---------------------------------------------------------------------------
# ServiceManager.ensure_ollama_running
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ServiceManager.probe_vllm_health
# ---------------------------------------------------------------------------


def test_probe_vllm_health_returns_true_when_up(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_resp

    with patch("http.client.HTTPConnection", return_value=mock_conn):
        result = mgr.probe_vllm_health()

    assert result is True
    mock_conn.request.assert_called_once_with("GET", "/health")


def test_probe_vllm_health_returns_false_and_logs_when_down(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch("http.client.HTTPConnection") as mock_conn_cls,
        patch("sys.platform", "linux"),  # non-Windows: no terminal spawn
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        result = mgr.probe_vllm_health()

    assert result is False


def test_probe_vllm_health_opens_terminal_on_windows(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch("http.client.HTTPConnection") as mock_conn_cls,
        patch("sys.platform", "win32"),
        patch("subprocess.Popen") as mock_popen,
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        mgr.probe_vllm_health()

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert "wt.exe" in cmd
    assert "wsl" in cmd


def test_probe_vllm_health_opens_terminal_only_once(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch("http.client.HTTPConnection") as mock_conn_cls,
        patch("sys.platform", "win32"),
        patch("subprocess.Popen") as mock_popen,
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        mgr.probe_vllm_health()
        mgr.probe_vllm_health()  # second call — terminal must not open again

    mock_popen.assert_called_once()


def test_probe_vllm_health_handles_missing_wt_exe(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch("http.client.HTTPConnection") as mock_conn_cls,
        patch("sys.platform", "win32"),
        patch("subprocess.Popen", side_effect=FileNotFoundError("wt.exe not found")),
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        mgr.probe_vllm_health()  # must not raise


# ---------------------------------------------------------------------------
# ServiceManager._litellm_serving / ensure_litellm_running
# ---------------------------------------------------------------------------


def test_litellm_serving_returns_true_when_http_responds(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    mock_conn = MagicMock()
    with patch("http.client.HTTPConnection", return_value=mock_conn):
        result = mgr._litellm_serving()
    assert result is True
    mock_conn.request.assert_called_once_with("GET", "/health")


def test_litellm_serving_returns_false_on_connection_error(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with patch("http.client.HTTPConnection") as mock_cls:
        mock_cls.return_value.request.side_effect = OSError("connection refused")
        result = mgr._litellm_serving()
    assert result is False


def test_ensure_litellm_running_skips_start_when_already_serving(
    tmp_path: Path,
) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch.object(mgr, "_litellm_serving", return_value=True),
        patch("subprocess.Popen") as mock_popen,
    ):
        mgr.ensure_litellm_running()
    mock_popen.assert_not_called()


def test_wait_for_litellm_ready_retries_until_serving(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    call_count = 0

    def _serving_side_effect() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count >= 3

    with (
        patch.object(mgr, "_litellm_serving", side_effect=_serving_side_effect),
        patch("time.sleep"),
    ):
        mgr._wait_for_litellm_ready()

    assert call_count == 3


def test_wait_for_litellm_ready_raises_when_proc_exits(tmp_path: Path) -> None:
    import pytest

    mgr = ServiceManager(tmp_path)
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.poll.return_value = 1
    mock_proc.returncode = 1
    mgr._litellm_proc = mock_proc

    with (
        patch.object(mgr, "_litellm_serving", return_value=False),
        patch("time.sleep"),
        pytest.raises(RuntimeError, match="exited"),
    ):
        mgr._wait_for_litellm_ready()


# ---------------------------------------------------------------------------
# ServiceManager._start_litellm_windows
# ---------------------------------------------------------------------------


def test_start_litellm_windows_opens_wt_tab(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with patch("subprocess.Popen") as mock_popen:
        mgr._start_litellm_windows(["litellm", "--config", "cfg.yaml"], {})

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "wt.exe"
    assert "new-tab" in cmd
    assert "cmd.exe" in cmd  # shell wrapper so PATHEXT resolves litellm.bat/.exe
    assert "/k" in cmd
    assert "litellm" in cmd[-1]  # shell_cmd string is last arg
    assert mgr._litellm_proc is None  # wt.exe exits immediately; not tracked


def test_start_litellm_windows_falls_back_to_console_when_no_wt(
    tmp_path: Path,
) -> None:
    mgr = ServiceManager(tmp_path)
    with patch(
        "subprocess.Popen",
        side_effect=[FileNotFoundError("wt.exe not found"), MagicMock()],
    ) as mock_popen:
        mgr._start_litellm_windows(["litellm", "--config", "cfg.yaml"], {})

    assert mock_popen.call_count == 2
    fallback_cmd = mock_popen.call_args[0][0]
    assert fallback_cmd[0] == "litellm"
    assert mgr._litellm_proc is not None


# ---------------------------------------------------------------------------
# ServiceManager.ensure_ollama_running
# ---------------------------------------------------------------------------


def test_ensure_ollama_running_already_up(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch("socket.socket") as mock_sock_cls,
        patch("subprocess.Popen") as mock_popen,
    ):
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 0  # already up
        mock_sock_cls.return_value = mock_sock

        mgr.ensure_ollama_running()

    mock_popen.assert_not_called()


def test_ensure_ollama_running_starts_process(tmp_path: Path) -> None:
    cfg = tmp_path / "litellm-local.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: claude-sonnet-4-6\n"
        "    litellm_params:\n"
        "      model: ollama_chat/qwen3-coder:30b\n"
        "      api_base: http://localhost:11434\n"
    )
    mgr = ServiceManager(tmp_path)

    call_count = 0

    def _probe_side_effect(*args: Any, **kwargs: Any) -> int:
        nonlocal call_count
        call_count += 1
        # First call (already-up check) -> not up; subsequent calls (wait loop) -> up
        return 1 if call_count == 1 else 0

    mock_resp = MagicMock()
    mock_resp.status = 200

    with (
        patch("socket.socket") as mock_sock_cls,
        patch("subprocess.Popen") as mock_popen,
        patch("http.client.HTTPConnection") as mock_conn_cls,
    ):
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.side_effect = _probe_side_effect
        mock_sock_cls.return_value = mock_sock

        mock_conn_cls.return_value.getresponse.return_value = mock_resp

        mgr.ensure_ollama_running()

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "ollama"
    assert cmd[1] == "run"
    assert cmd[2] == "qwen3-coder:30b"
    assert "--keepalive" in cmd
    assert "120m" in cmd


def test_wait_for_ollama_ready_shutdown_interrupt(tmp_path: Path) -> None:
    """RuntimeError raised promptly when _running is False before the call."""
    mgr = ServiceManager(tmp_path)
    mgr._running = False

    import pytest

    with pytest.raises(RuntimeError, match="shutting down"):
        mgr._wait_for_ollama_ready()


def test_wait_for_ollama_ready_http_retries(tmp_path: Path) -> None:
    """HTTP /api/tags is retried until it returns 200."""
    mgr = ServiceManager(tmp_path)
    http_call_count = 0

    def _conn_side_effect(*args: Any, **kwargs: Any) -> Any:
        nonlocal http_call_count
        http_call_count += 1
        mock_conn = MagicMock()
        if http_call_count < 3:
            mock_conn.getresponse.side_effect = OSError("service not ready yet")
        else:
            mock_conn.getresponse.return_value = MagicMock(status=200)
        return mock_conn

    with (
        patch("socket.socket") as mock_sock_cls,
        patch("http.client.HTTPConnection", side_effect=_conn_side_effect),
        patch("time.sleep"),
    ):
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect_ex.return_value = 0  # TCP always accepts
        mock_sock_cls.return_value = mock_sock

        mgr._wait_for_ollama_ready()

    assert http_call_count == 3
