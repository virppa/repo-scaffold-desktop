"""Tests for app.core.watcher.watcher_services (ServiceManager).

WOR-368 retired the LiteLLM proxy and Ollama plumbing; ServiceManager now
only gates vLLM readiness. The previous suite's LiteLLM/Ollama tests were
removed because the underlying methods no longer exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.watcher.watcher_services import _VLLM_FP8_CMD, ServiceManager

# ---------------------------------------------------------------------------
# ServiceManager.stop  (no-op kept for call-site compat)
# ---------------------------------------------------------------------------


def test_stop_is_noop_and_marks_not_running(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    assert mgr._running is True
    mgr.stop()  # must not raise — no proc to terminate post-WOR-368
    assert mgr._running is False


def test_stop_is_idempotent(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    mgr.stop()
    mgr.stop()  # must not raise on repeated call


# ---------------------------------------------------------------------------
# ServiceManager.probe_vllm_health  (cheap soft-check; logs and continues)
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
    mock_conn.request.assert_called_once_with("GET", "/v1/models")


def test_probe_vllm_health_returns_false_and_logs_when_down(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch("http.client.HTTPConnection") as mock_conn_cls,
        patch("sys.platform", "linux"),  # non-Windows: no terminal spawn
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        result = mgr.probe_vllm_health()

    assert result is False
    assert mgr._vllm_warned is True


def test_probe_vllm_health_logs_short_message_on_repeat_failure(
    tmp_path: Path, caplog: Any
) -> None:
    import logging

    mgr = ServiceManager(tmp_path)
    with (
        patch("http.client.HTTPConnection") as mock_conn_cls,
        patch("sys.platform", "linux"),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_services"),
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        mgr.probe_vllm_health()  # first call — logs full command
        caplog.clear()
        mgr.probe_vllm_health()  # second call — short message only

    # Full vLLM command should NOT appear on the second call
    assert _VLLM_FP8_CMD not in caplog.text


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
# ServiceManager.ensure_vllm_anthropic_mode
#
# Strict pre-dispatch gate: probes /v1/models AND /v1/messages, raises with
# the launch command embedded if either fails. Replaces ensure_litellm_running
# (deleted in WOR-368) — vLLM 0.20.0 mounts /v1/messages natively, so the
# watcher only needs to confirm the destination is live, not spawn a proxy.
# ---------------------------------------------------------------------------


def _models_ok_response(model_id: str = "qwen3-coder") -> MagicMock:
    """Build a /v1/models 200 response listing the served model."""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps({"data": [{"id": model_id}]}).encode("utf-8")
    return resp


def _messages_ok_response() -> MagicMock:
    """Build a /v1/messages 200 response in valid Anthropic message shape."""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps(
        {
            "type": "message",
            "content": [{"type": "text", "text": "ok"}],
        }
    ).encode("utf-8")
    return resp


def test_ensure_vllm_anthropic_mode_passes_when_both_probes_ok(
    tmp_path: Path,
) -> None:
    mgr = ServiceManager(tmp_path)
    responses = [_models_ok_response(), _messages_ok_response()]
    mock_conn = MagicMock()
    mock_conn.getresponse.side_effect = responses

    with patch("http.client.HTTPConnection", return_value=mock_conn):
        mgr.ensure_vllm_anthropic_mode()  # must not raise


def test_ensure_vllm_anthropic_mode_raises_when_models_endpoint_down(
    tmp_path: Path,
) -> None:
    mgr = ServiceManager(tmp_path)
    with patch("http.client.HTTPConnection") as mock_conn_cls:
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        with pytest.raises(RuntimeError, match="vLLM not serving"):
            mgr.ensure_vllm_anthropic_mode()


def test_ensure_vllm_anthropic_mode_raises_when_served_model_missing(
    tmp_path: Path,
) -> None:
    """If /v1/models lists models but not _VLLM_SERVED_MODEL, dispatch must abort."""
    mgr = ServiceManager(tmp_path)
    bad_models = MagicMock()
    bad_models.status = 200
    bad_models.read.return_value = json.dumps(
        {"data": [{"id": "some-other-model"}]}
    ).encode("utf-8")
    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = bad_models

    with patch("http.client.HTTPConnection", return_value=mock_conn):
        with pytest.raises(RuntimeError, match="vLLM not serving"):
            mgr.ensure_vllm_anthropic_mode()


def test_ensure_vllm_anthropic_mode_raises_when_messages_endpoint_returns_500(
    tmp_path: Path,
) -> None:
    """vLLM up on /v1/models but Anthropic router returns 5xx → distinct error."""
    mgr = ServiceManager(tmp_path)
    bad_messages = MagicMock()
    bad_messages.status = 500
    bad_messages.read.return_value = b'{"error": "internal"}'
    mock_conn = MagicMock()
    mock_conn.getresponse.side_effect = [_models_ok_response(), bad_messages]

    with patch("http.client.HTTPConnection", return_value=mock_conn):
        with pytest.raises(RuntimeError, match="Anthropic router"):
            mgr.ensure_vllm_anthropic_mode()


def test_ensure_vllm_anthropic_mode_raises_when_messages_returns_wrong_shape(
    tmp_path: Path,
) -> None:
    """200 OK but payload missing type=message → still fails the gate."""
    mgr = ServiceManager(tmp_path)
    wrong_shape = MagicMock()
    wrong_shape.status = 200
    wrong_shape.read.return_value = json.dumps({"choices": []}).encode("utf-8")
    mock_conn = MagicMock()
    mock_conn.getresponse.side_effect = [_models_ok_response(), wrong_shape]

    with patch("http.client.HTTPConnection", return_value=mock_conn):
        with pytest.raises(RuntimeError, match="Anthropic router"):
            mgr.ensure_vllm_anthropic_mode()


def test_ensure_vllm_anthropic_mode_error_message_includes_launch_command(
    tmp_path: Path,
) -> None:
    """Operator-facing: the failure must surface the vLLM launch command."""
    mgr = ServiceManager(tmp_path)
    with patch("http.client.HTTPConnection") as mock_conn_cls:
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        with pytest.raises(RuntimeError) as exc_info:
            mgr.ensure_vllm_anthropic_mode()
    assert "vllm serve" in str(exc_info.value)
    assert "qwen3-coder" in str(exc_info.value)
