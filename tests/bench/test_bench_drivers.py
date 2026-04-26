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


_OLLAMA_SHOW_BODY = json.dumps(
    {
        "modelfile": "FROM ...",
        "details": {
            "format": "gguf",
            "family": "qwen2",
            "parameter_size": "30.5B",
            "quantization_level": "Q4_K_M",
        },
    }
).encode()


def test_fetch_model_info_returns_quant_family_and_param_count():
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_SHOW_BODY)):
        driver = OllamaDriver()
        info = driver.fetch_model_info("qwen3-coder:30b")

    assert info["model_quant"] == "Q4_K_M"
    assert info["model_family"] == "qwen2"
    assert info["model_param_count"] == "30.5B"


def test_fetch_model_info_returns_none_on_network_error():
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        driver = OllamaDriver()
        info = driver.fetch_model_info("some-model:7b")

    assert info["model_quant"] is None
    assert info["model_family"] is None
    assert info["model_param_count"] is None


def test_fetch_model_info_returns_none_when_details_missing():
    body = json.dumps({"modelfile": "FROM ..."}).encode()
    with patch("urllib.request.urlopen", _mock_urlopen(body)):
        driver = OllamaDriver()
        info = driver.fetch_model_info("some-model:7b")

    assert info["model_quant"] is None
    assert info["model_family"] is None
    assert info["model_param_count"] is None


def test_fetch_model_info_quantization_level_none_when_empty_string():
    body = json.dumps(
        {"details": {"quantization_level": "", "parameter_size": "7B"}}
    ).encode()
    with patch("urllib.request.urlopen", _mock_urlopen(body)):
        info = OllamaDriver().fetch_model_info("some-model:7b")

    assert info["model_quant"] is None
    assert info["model_param_count"] == "7B"
    assert info["model_family"] is None


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


def test_ollama_generate_cold_start_populates_duration_fields():
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_COLD_BODY)):
        driver = OllamaDriver()
        result = driver.generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    # prompt_eval_duration_s = 100_000_000 ns / 1e9
    assert result.prompt_eval_duration_s == pytest.approx(0.1)
    # load_duration_s = 2_000_000 ns / 1e9
    assert result.load_duration_s == pytest.approx(0.002)
    # decode_time_s = 500_000_000 ns / 1e9
    assert result.decode_time_s == pytest.approx(0.5)


def test_ollama_generate_warm_start_load_duration_s_is_none():
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_WARM_BODY)):
        driver = OllamaDriver()
        result = driver.generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.load_duration_s is None
    assert result.prompt_eval_duration_s == pytest.approx(0.05)


def test_ollama_generate_cache_state_none_when_absent():
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_COLD_BODY)):
        result = OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.cache_state is None


def test_ollama_generate_cache_state_captured_from_response():
    body = _ndjson(
        {
            "model": "q",
            "message": {"role": "assistant", "content": "Hi"},
            "done": False,
        },
        {
            "model": "q",
            "done": True,
            "cache_state": "loaded",
            "load_duration": 0,
            "prompt_eval_count": 5,
            "prompt_eval_duration": 50_000_000,
            "eval_count": 10,
            "eval_duration": 200_000_000,
        },
    )
    with patch("urllib.request.urlopen", _mock_urlopen(body)):
        result = OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.cache_state == "loaded"


def test_ollama_non_thinking_model_ttfut_is_none():
    """Non-thinking models never emit </think>, so ttfut_s must remain None."""
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_COLD_BODY)):
        result = OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.ttfut_s is None


_OLLAMA_THINKING_SAME_CHUNK = _ndjson(
    {
        "model": "q",
        "message": {"role": "assistant", "content": "</think>Answer"},
        "done": False,
    },
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


def test_ollama_thinking_model_ttfut_set_when_answer_in_same_chunk():
    """ttfut_s is set when </think> and the first answer token are in the same chunk."""
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_THINKING_SAME_CHUNK)):
        result = OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.ttfut_s is not None and result.ttfut_s >= 0.0
    assert result.ttft_s is not None


_OLLAMA_THINKING_SEPARATE_CHUNKS = _ndjson(
    {
        "model": "q",
        "message": {"role": "assistant", "content": "<think>reasoning"},
        "done": False,
    },
    {
        "model": "q",
        "message": {"role": "assistant", "content": "</think>"},
        "done": False,
    },
    {
        "model": "q",
        "message": {"role": "assistant", "content": "The answer"},
        "done": False,
    },
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


def test_ollama_thinking_model_ttfut_set_after_think_tag_separate_chunk():
    """ttfut_s is set on the first content chunk that follows </think>."""
    with patch(
        "urllib.request.urlopen", _mock_urlopen(_OLLAMA_THINKING_SEPARATE_CHUNKS)
    ):
        result = OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.ttfut_s is not None and result.ttfut_s >= 0.0


_OLLAMA_THINKING_SPLIT_TAG = _ndjson(
    {
        "model": "q",
        "message": {"role": "assistant", "content": "<think>reasoning</thi"},
        "done": False,
    },
    {
        "model": "q",
        "message": {"role": "assistant", "content": "nk>"},
        "done": False,
    },
    {
        "model": "q",
        "message": {"role": "assistant", "content": "The answer"},
        "done": False,
    },
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


def test_ollama_thinking_model_ttfut_detects_split_think_tag():
    """</think> split across two chunk boundaries is still detected correctly."""
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_THINKING_SPLIT_TAG)):
        result = OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.ttfut_s is not None and result.ttfut_s >= 0.0


_OLLAMA_THINKING_EMPTY_CONTENT = _ndjson(
    {"model": "q", "message": {"role": "assistant", "content": ""}, "done": False},
    {"model": "q", "message": {"role": "assistant", "content": ""}, "done": False},
    {
        "model": "q",
        "message": {"role": "assistant", "content": "</think>"},
        "done": False,
    },
    {
        "model": "q",
        "message": {"role": "assistant", "content": "Hello"},
        "done": False,
    },
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


def test_ollama_thinking_model_empty_content_during_thinking_then_ttfut():
    """Empty content chunks during thinking phase; ttfut_s set after </think>."""
    with patch("urllib.request.urlopen", _mock_urlopen(_OLLAMA_THINKING_EMPTY_CONTENT)):
        result = OllamaDriver().generate(
            "q", [{"role": "user", "content": "hi"}], 4096, 256, 0.7, None
        )

    assert result.ttfut_s is not None and result.ttfut_s >= 0.0


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
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value = io.BytesIO(b"ok")
        driver = VllmDriver()
        assert driver.is_available() is True
        assert mock_open.call_args[0][0].full_url.endswith("/v1/models")
