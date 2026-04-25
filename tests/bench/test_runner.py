"""Tests for benchmark runner helpers."""

from __future__ import annotations

import pytest

from scripts.bench.drivers.ollama import OllamaDriver
from scripts.bench.drivers.vllm import VllmDriver
from scripts.bench.runner import _case_id, _is_oom, _make_driver, _make_prompt
from scripts.bench.tasks import BenchPrompt

# ---------------------------------------------------------------------------
# _is_oom
# ---------------------------------------------------------------------------


def test_is_oom_detects_507() -> None:
    assert _is_oom("HTTP 507 Insufficient Storage") is True


def test_is_oom_detects_out_of_memory() -> None:
    assert _is_oom("CUDA out of memory") is True


def test_is_oom_case_insensitive_oom() -> None:
    assert _is_oom("OUT OF MEMORY") is True


def test_is_oom_detects_connection_reset() -> None:
    assert _is_oom("connection reset by peer") is True


def test_is_oom_detects_connectionreset_camel() -> None:
    assert _is_oom("ConnectionReset") is True


def test_is_oom_returns_false_for_normal_error() -> None:
    assert _is_oom("timeout waiting for response") is False


def test_is_oom_returns_false_for_empty_string() -> None:
    assert _is_oom("") is False


# ---------------------------------------------------------------------------
# _make_driver
# ---------------------------------------------------------------------------


def test_make_driver_returns_vllm_for_vllm_backend() -> None:
    assert isinstance(_make_driver("vllm-local", "http://localhost:8000"), VllmDriver)


def test_make_driver_vllm_match_is_case_insensitive() -> None:
    assert isinstance(_make_driver("VLLM", "http://localhost:8000"), VllmDriver)


def test_make_driver_returns_ollama_for_ollama_backend() -> None:
    assert isinstance(_make_driver("ollama", "http://localhost:11434"), OllamaDriver)


def test_make_driver_returns_ollama_as_default() -> None:
    assert isinstance(_make_driver("local", "http://localhost:11434"), OllamaDriver)


# ---------------------------------------------------------------------------
# _make_prompt
# ---------------------------------------------------------------------------


def test_make_prompt_speed() -> None:
    prompt = _make_prompt("speed", 0)
    assert isinstance(prompt, BenchPrompt)
    assert prompt.task_type == "speed"


def test_make_prompt_coding() -> None:
    prompt = _make_prompt("coding", 0)
    assert isinstance(prompt, BenchPrompt)
    assert prompt.task_type == "coding"


def test_make_prompt_boundary() -> None:
    prompt = _make_prompt("boundary", 0)
    assert isinstance(prompt, BenchPrompt)
    assert prompt.task_type == "boundary"


def test_make_prompt_raises_for_unknown_tier() -> None:
    with pytest.raises(ValueError, match="Unknown tier"):
        _make_prompt("nonexistent", 0)


# ---------------------------------------------------------------------------
# _case_id
# ---------------------------------------------------------------------------


def test_case_id_format() -> None:
    from unittest.mock import MagicMock

    case = MagicMock()
    case.backend_id = "ollama"
    case.model_id = "qwen3:30b"
    case.tier = "speed"
    case.context_size = 4096
    case.concurrency = 1
    assert _case_id(case) == "ollama/qwen3:30b/speed/4096/1"
