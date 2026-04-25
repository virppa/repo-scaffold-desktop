"""Tests for OllamaManager lifecycle methods — all network calls mocked."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.bench.lifecycle.ollama_manager import ModelStatus, OllamaManager


def _mock_urlopen(body: bytes) -> MagicMock:
    return MagicMock(return_value=io.BytesIO(body))


_PS_RESPONSE = json.dumps(
    {
        "models": [
            {
                "name": "llama3:latest",
                "model": "llama3:latest",
                "size": 4_000_000_000,
                "digest": "abc123",
                "details": {},
                "expires_at": "2024-12-31T23:59:59Z",
                "size_vram": 4_000_000_000,
                "processor": "100% GPU",
            }
        ]
    }
).encode()

_TAGS_EMPTY = json.dumps({"models": []}).encode()
_TAGS_WITH_MODEL = json.dumps({"models": [{"name": "llama3:latest"}]}).encode()
_PULL_RESPONSE = json.dumps({"status": "success"}).encode()
_FLUSH_RESPONSE = json.dumps({}).encode()


# ---------------------------------------------------------------------------
# get_ps_status
# ---------------------------------------------------------------------------


def test_get_ps_status_returns_model_status():
    with patch("urllib.request.urlopen", _mock_urlopen(_PS_RESPONSE)):
        manager = OllamaManager()
        status = manager.get_ps_status("llama3:latest")

    assert isinstance(status, ModelStatus)
    assert status.name == "llama3:latest"
    assert status.size == 4_000_000_000
    assert status.size_vram == 4_000_000_000
    assert status.processor_status == "100% GPU"
    assert status.expires_at == "2024-12-31T23:59:59Z"


def test_get_ps_status_returns_none_when_not_loaded():
    with patch(
        "urllib.request.urlopen", _mock_urlopen(json.dumps({"models": []}).encode())
    ):
        manager = OllamaManager()
        assert manager.get_ps_status("llama3:latest") is None


def test_get_ps_status_processor_status_defaults_to_unknown():
    body = json.dumps(
        {
            "models": [
                {
                    "name": "phi3:latest",
                    "size": 1_000_000,
                    "size_vram": 0,
                    # no 'processor' key
                }
            ]
        }
    ).encode()
    with patch("urllib.request.urlopen", _mock_urlopen(body)):
        manager = OllamaManager()
        status = manager.get_ps_status("phi3:latest")

    assert status is not None
    assert status.processor_status == "unknown"


# ---------------------------------------------------------------------------
# flush_model
# ---------------------------------------------------------------------------


def test_flush_model_sends_post_with_keep_alive_zero():
    mock_urlopen = _mock_urlopen(_FLUSH_RESPONSE)
    with patch("urllib.request.urlopen", mock_urlopen):
        manager = OllamaManager()
        manager.flush_model("llama3:latest")

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://localhost:11434/api/generate"
    body = json.loads(req.data)
    assert body["model"] == "llama3:latest"
    assert body["keep_alive"] == 0


# ---------------------------------------------------------------------------
# pull_if_needed
# ---------------------------------------------------------------------------


def test_pull_if_needed_skips_when_model_already_present():
    mock_urlopen = _mock_urlopen(_TAGS_WITH_MODEL)
    with patch("urllib.request.urlopen", mock_urlopen):
        manager = OllamaManager()
        manager.pull_if_needed("llama3:latest")

    # Only the /api/tags GET should be called; no /api/pull
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert "/api/tags" in req.full_url


def test_pull_if_needed_pulls_when_model_missing():
    call_count = 0
    responses = [_TAGS_EMPTY, _PULL_RESPONSE]

    def side_effect(req, timeout=None):  # type: ignore[no-untyped-def]
        nonlocal call_count
        body = responses[call_count]
        call_count += 1
        return io.BytesIO(body)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        manager = OllamaManager()
        manager.pull_if_needed("llama3:latest")

    assert call_count == 2  # tags check + pull


# ---------------------------------------------------------------------------
# ensure_running
# ---------------------------------------------------------------------------


def test_ensure_running_returns_immediately_when_port_open():
    mock_sock = MagicMock()
    mock_sock.__enter__ = MagicMock(return_value=mock_sock)
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.connect_ex.return_value = 0

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_resp

    with (
        patch("socket.socket", return_value=mock_sock),
        patch("http.client.HTTPConnection", return_value=mock_conn),
    ):
        manager = OllamaManager()
        manager.ensure_running(timeout=5.0)

    mock_sock.connect_ex.assert_called_once()
    mock_conn.request.assert_called_once_with("GET", "/api/tags")


def test_ensure_running_raises_timeout_when_port_never_opens():
    mock_sock = MagicMock()
    mock_sock.__enter__ = MagicMock(return_value=mock_sock)
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_sock.connect_ex.return_value = 1  # always closed

    with patch("socket.socket", return_value=mock_sock), patch("time.sleep"):
        manager = OllamaManager()
        with pytest.raises(TimeoutError):
            manager.ensure_running(timeout=0.01)
