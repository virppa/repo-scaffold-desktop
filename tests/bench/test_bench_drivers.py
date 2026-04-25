"""Tests for OllamaDriver and VllmDriver — all network calls mocked."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from scripts.bench.drivers.ollama import OllamaDriver
from scripts.bench.drivers.vllm import VllmDriver


def _ndjson(*frames: dict) -> bytes:
    return b"\n".join(json.dumps(f).encode() for f in frames) + b"\n"


def _sse(*frames: dict | str) -> bytes:
    lines: list[bytes] = []
    for frame in frames:
        if isinstance(frame, str):
            lines.append(f"data: {frame}\n\n".encode())
        else:
            lines.append(f"data: {json.dumps(frame)}\n\n".encode())
    return b"".join(lines)


def _mock_urlopen(body: bytes) -> MagicMock:
    """Return a mock for urllib.request.urlopen that yields body as a BytesIO."""
    mock = MagicMock(return_value=io.BytesIO(body))
    return mock


def _captured_payload(mock_open: MagicMock) -> dict:
    """Extract and decode the JSON payload sent to urlopen."""
    return json.loads(mock_open.call_args[0][0].data)


# ---------------------------------------------------------------------------
# OllamaDriver
# ---------------------------------------------------------------------------

_OLLAMA_COLD_BODY = _ndjson(
    {"model": "q", "message": {"role": "assistant", "content": "Hello"}, "done": False},
    {
        "model": "q",
        "message": {"role": "assistant", "content": " world"},
        "done": False,
    },
    {
        "model": "q",
        "done": True,
        "done_reason": "stop",
        "load_duration": 2_000_000,
        "prompt_eval_count": 10,
        "prompt_eval_duration": 100_000_000,
        "eval_count": 20,
        "eval_duration": 500_000_000,
    },
)

_OLLAMA_WARM_BODY = _ndjson(
    {"model": "q", "message": {"role": "assistant", "content": "Hi"}, "done": False},
    {
        "model": "q",
        "done": True,
        "done_reason": "stop",
        "load_duration": 0,
        "prompt_eval_count": 5,
        "prompt_eval_duration": 50_000_000,
        "eval_count": 10,
        "eval_duration": 200_000_000,
    },
)


def test_ollama_generate_cold_start_timings():
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_COLD_BODY)):
        driver = OllamaDriver()
        result = driver.generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.error is None
    assert result.text == "Hello world"
    assert result.ttft_s is not None and result.ttft_s >= 0.0
    assert result.raw_prompt_eval_duration_ns == 100_000_000
    assert result.raw_eval_duration_ns == 500_000_000
    assert result.decode_time_s == pytest.approx(0.5)
    assert result.raw_load_duration_ns == 2_000_000
    assert result.input_tokens == 10
    assert result.output_tokens == 20


def test_ollama_generate_warm_start_no_load_duration():
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_WARM_BODY)):
        driver = OllamaDriver()
        result = driver.generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.error is None
    assert result.raw_load_duration_ns is None
    assert result.raw_eval_duration_ns == 200_000_000
    assert result.decode_time_s == pytest.approx(0.2)
    assert result.input_tokens == 5
    assert result.output_tokens == 10


def test_ollama_generate_returns_error_on_url_error():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        driver = OllamaDriver()
        result = driver.generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.error is not None
    assert "Connection refused" in result.error


def test_ollama_generate_returns_error_on_generic_exception():
    with patch("urllib.request.urlopen", side_effect=OSError("socket error")):
        driver = OllamaDriver()
        result = driver.generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.error is not None


def test_ollama_generate_payload_contains_num_predict_and_temperature():
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = io.BytesIO(_OLLAMA_WARM_BODY)
        OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 32768, 256, 0.7, None
        )
        payload = _captured_payload(mock_open)

    assert payload["options"]["num_ctx"] == 32768
    assert payload["options"]["num_predict"] == 256
    assert payload["options"]["temperature"] == pytest.approx(0.7)
    assert "seed" not in payload["options"]


def test_ollama_generate_payload_includes_seed_when_set():
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = io.BytesIO(_OLLAMA_WARM_BODY)
        OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 512, 0.0, 42
        )
        payload = _captured_payload(mock_open)

    assert payload["options"]["temperature"] == pytest.approx(0.0)
    assert payload["options"]["num_predict"] == 512
    assert payload["options"]["seed"] == 42


def test_ollama_generate_payload_omits_seed_when_none():
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = io.BytesIO(_OLLAMA_WARM_BODY)
        OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 128, 0.7, None
        )
        payload = _captured_payload(mock_open)

    assert "seed" not in payload["options"]


def test_ollama_is_available_returns_false_when_unreachable():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        driver = OllamaDriver()
        assert driver.is_available() is False


def test_ollama_is_available_returns_true_when_reachable():
    with patch("urllib.request.urlopen", _mock_urlopen(b"{}")):
        driver = OllamaDriver()
        assert driver.is_available() is True


# ---------------------------------------------------------------------------
# VllmDriver
# ---------------------------------------------------------------------------

_VLLM_BODY = _sse(
    {
        "id": "c1",
        "choices": [
            {"delta": {"role": "assistant", "content": ""}, "finish_reason": None}
        ],
    },
    {
        "id": "c1",
        "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}],
    },
    {
        "id": "c1",
        "choices": [{"delta": {"content": " world"}, "finish_reason": None}],
    },
    {
        "id": "c1",
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    },
    "[DONE]",
)


def test_vllm_generate_success():
    with patch("urllib.request.urlopen", _mock_urlopen(_VLLM_BODY)):
        driver = VllmDriver()
        result = driver.generate(
            "mistral", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.error is None
    assert result.text == "Hello world"
    assert result.ttft_s is not None and result.ttft_s >= 0.0
    assert result.input_tokens == 10
    assert result.output_tokens == 20


def test_vllm_generate_returns_error_on_url_error():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        driver = VllmDriver()
        result = driver.generate(
            "mistral", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.error is not None
    assert "Connection refused" in result.error


def test_vllm_generate_returns_error_on_generic_exception():
    with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        driver = VllmDriver()
        result = driver.generate(
            "mistral", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.error is not None


def test_vllm_generate_payload_contains_max_tokens_and_temperature():
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = io.BytesIO(_VLLM_BODY)
        VllmDriver().generate(
            "m", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )
        payload = _captured_payload(mock_open)

    assert payload["max_tokens"] == 256
    assert payload["temperature"] == pytest.approx(0.7)
    assert "seed" not in payload


def test_vllm_generate_payload_includes_seed_when_set():
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = io.BytesIO(_VLLM_BODY)
        VllmDriver().generate(
            "m", [{"role": "user", "content": "hi"}], 4096, 512, 0.0, 42
        )
        payload = _captured_payload(mock_open)

    assert payload["max_tokens"] == 512
    assert payload["temperature"] == pytest.approx(0.0)
    assert payload["seed"] == 42


def test_vllm_is_available_returns_false_when_unreachable():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        driver = VllmDriver()
        assert driver.is_available() is False


def test_vllm_is_available_returns_true_when_reachable():
    with patch("urllib.request.urlopen", _mock_urlopen(b"ok")):
        driver = VllmDriver()
        assert driver.is_available() is True
