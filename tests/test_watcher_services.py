"""Tests for app.core.watcher.watcher_services (ServiceManager).

WOR-368 retired the LiteLLM proxy and Ollama plumbing; ServiceManager now
only gates vLLM readiness. The previous suite's LiteLLM/Ollama tests were
removed because the underlying methods no longer exist.
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404 — used for CalledProcessError type only
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.watcher.watcher_services import (
    _VLLM_AUTOSTART_CMD,
    _VLLM_FP8_CMD,
    _VLLM_SCRIPT_BODY,
    _VLLM_SCRIPT_PATH,
    ServiceManager,
    _write_vllm_script_file,
)

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
        patch(
            "app.core.watcher.watcher_services._write_vllm_script_file"
        ) as mock_write,
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        mgr.probe_vllm_health()

    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert "wt.exe" in cmd
    assert "wsl" in cmd
    # WOR-415: script file must be written before the terminal spawns
    mock_write.assert_called_once()


def test_probe_vllm_health_opens_terminal_only_once(tmp_path: Path) -> None:
    mgr = ServiceManager(tmp_path)
    with (
        patch("http.client.HTTPConnection") as mock_conn_cls,
        patch("sys.platform", "win32"),
        patch("subprocess.Popen") as mock_popen,
        patch("app.core.watcher.watcher_services._write_vllm_script_file"),
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
        patch("app.core.watcher.watcher_services._write_vllm_script_file"),
    ):
        mock_conn_cls.return_value.request.side_effect = OSError("connection refused")
        mgr.probe_vllm_health()  # must not raise


# ---------------------------------------------------------------------------
# WOR-415 (script-file approach): preserve_thinking via on-disk bash script
# ---------------------------------------------------------------------------


def test_vllm_autostart_cmd_invokes_script_only() -> None:
    """The auto-start command passed to wt.exe → wsl → bash must be JUST
    `bash <script>` — no JSON literal anywhere in the multi-shell argv chain.
    This is the entire point of the script-file approach: the JSON travels
    only via the on-disk script that bash reads directly."""
    assert _VLLM_AUTOSTART_CMD == f"bash {_VLLM_SCRIPT_PATH}"
    # Sanity: no JSON, no quoting tricks
    assert "preserve_thinking" not in _VLLM_AUTOSTART_CMD
    assert "$(" not in _VLLM_AUTOSTART_CMD


def test_vllm_script_body_contains_full_invocation_with_preserve_thinking() -> None:
    """The script body must be a complete vLLM serve command including the
    preserve_thinking kwarg as a single-quoted JSON literal. Bash parses
    single-quoted strings literally, so the JSON survives intact when bash
    reads the script from disk."""
    assert _VLLM_SCRIPT_BODY.startswith("#!/bin/bash\n")
    assert "vllm serve" in _VLLM_SCRIPT_BODY
    assert "qwen3-coder" in _VLLM_SCRIPT_BODY
    # Critical: JSON literal must be single-quoted in the script
    assert "--default-chat-template-kwargs '{\"preserve_thinking\": true}'" in (
        _VLLM_SCRIPT_BODY
    )


def test_vllm_fp8_cmd_is_canonical_full_command() -> None:
    """_VLLM_FP8_CMD remains the canonical full command (used in operator-
    facing warning logs and matches the version in CLAUDE.md). It is NOT
    the auto-start command — that is _VLLM_AUTOSTART_CMD."""
    assert "vllm serve" in _VLLM_FP8_CMD
    # The canonical command DOES include the JSON literal — it's meant for
    # operators to copy-paste into a single bash shell where JSON quoting
    # works fine.
    assert "preserve_thinking" in _VLLM_FP8_CMD
    # And it is NOT the auto-start command
    assert _VLLM_FP8_CMD != _VLLM_AUTOSTART_CMD


def test_vllm_cmd_includes_gpu_memory_utilization_fix() -> None:
    """WOR-527: --gpu-memory-utilization 0.95 must be present in both
    canonical command strings. WOR-336 found vLLM's default (0.90)
    under-provisions the KV pool for concurrent worker traffic; the 0.95
    bump grows the pool to reduce prefix-cache eviction pressure."""
    assert "--gpu-memory-utilization 0.95" in _VLLM_FP8_CMD
    assert "--gpu-memory-utilization 0.95" in _VLLM_SCRIPT_BODY


def test_write_vllm_script_file_pipes_body_via_stdin() -> None:
    """Writing must pipe the script body via stdin to `wsl bash -c '... tee
    ...'` — never a shell command interpolating the script content. This is
    what makes the JSON content survive: tee writes stdin bytes literally to
    the file."""
    with patch("subprocess.run") as mock_run:
        _write_vllm_script_file()

    mock_run.assert_called_once()
    call = mock_run.call_args
    argv = call[0][0]
    assert argv[:3] == ["wsl", "bash", "-c"]
    bash_cmd = argv[3]
    # The bash command references the path and uses tee + chmod, but the
    # script content (with JSON) does NOT appear in the command string.
    assert _VLLM_SCRIPT_PATH in bash_cmd
    assert "tee" in bash_cmd
    assert "chmod +x" in bash_cmd  # script must be executable
    assert "mkdir -p" in bash_cmd  # cache dir auto-created
    assert "preserve_thinking" not in bash_cmd  # JSON travels via stdin only
    # Script body arrives via stdin
    assert call.kwargs["input"] == _VLLM_SCRIPT_BODY.encode("utf-8")
    assert call.kwargs.get("check") is True


def test_write_vllm_script_file_handles_missing_wsl(caplog: Any) -> None:
    """If wsl.exe is not installed, log a warning and continue."""
    import logging

    with (
        patch("subprocess.run", side_effect=FileNotFoundError("wsl.exe missing")),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_services"),
    ):
        _write_vllm_script_file()  # must not raise

    assert "wsl.exe not found" in caplog.text


def test_write_vllm_script_file_handles_tee_failure(caplog: Any) -> None:
    """If the bash command fails (CalledProcessError), log and continue."""
    import logging

    err = subprocess.CalledProcessError(1, ["wsl", "bash", "-c", "..."])
    with (
        patch("subprocess.run", side_effect=err),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_services"),
    ):
        _write_vllm_script_file()  # must not raise

    assert "Could not write" in caplog.text


def test_open_vllm_terminal_writes_script_before_spawning(tmp_path: Path) -> None:
    """Order of operations must be: write script file FIRST, then spawn the
    terminal. If we spawn first, the terminal could try to invoke the script
    before it's written (race), and bash would fail at startup."""
    mgr = ServiceManager(tmp_path)
    call_order: list[str] = []

    def record_write() -> None:
        call_order.append("write")

    def record_popen(*args: Any, **kwargs: Any) -> MagicMock:
        call_order.append("popen")
        return MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_services._write_vllm_script_file",
            side_effect=record_write,
        ),
        patch("subprocess.Popen", side_effect=record_popen),
    ):
        mgr._open_vllm_terminal()

    assert call_order == ["write", "popen"], (
        f"expected write before popen, got {call_order}"
    )


def test_open_vllm_terminal_spawn_uses_script_invocation(tmp_path: Path) -> None:
    """The argv passed to wt.exe → wsl → bash must invoke the script — not
    pass any JSON content as an argument. This is the contract the script-
    file approach restores after WOR-408 / first-WOR-415 failed."""
    mgr = ServiceManager(tmp_path)
    with (
        patch("app.core.watcher.watcher_services._write_vllm_script_file"),
        patch("subprocess.Popen") as mock_popen,
    ):
        mgr._open_vllm_terminal()

    argv = mock_popen.call_args[0][0]
    # The last arg is the bash -c body — must be the autostart cmd, NOT
    # the full vLLM command with JSON.
    assert argv[-1] == _VLLM_AUTOSTART_CMD
    assert "preserve_thinking" not in argv[-1]


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
