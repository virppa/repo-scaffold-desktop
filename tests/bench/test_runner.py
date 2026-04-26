"""Tests for benchmark runner helpers."""

from __future__ import annotations

import pytest

from scripts.bench.config import BenchCase
from scripts.bench.drivers.ollama import OllamaDriver
from scripts.bench.drivers.vllm import VllmDriver
from scripts.bench.runner import (
    _case_id,
    _is_oom,
    _make_driver,
    _make_prompt,
    _make_skipped_run,
    _should_skip_concurrency_gate,
    _should_skip_oom,
    _update_adaptive_state,
)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _case(
    context_size: int = 4096,
    concurrency: int = 1,
    model_id: str = "model-1",
    backend_id: str = "ollama",
) -> BenchCase:
    return BenchCase(
        backend_id=backend_id,
        model_id=model_id,
        tier="speed",
        context_size=context_size,
        concurrency=concurrency,
        repeat_index=1,
    )


# ---------------------------------------------------------------------------
# _should_skip_oom
# ---------------------------------------------------------------------------


def test_skip_oom_skips_larger_ctx() -> None:
    oom: dict[tuple[str, str], int] = {("model-1", "ollama"): 4096}
    assert _should_skip_oom(_case(context_size=8192), oom, True) is True


def test_skip_oom_does_not_skip_same_ctx() -> None:
    oom: dict[tuple[str, str], int] = {("model-1", "ollama"): 4096}
    assert _should_skip_oom(_case(context_size=4096), oom, True) is False


def test_skip_oom_does_not_skip_smaller_ctx() -> None:
    oom: dict[tuple[str, str], int] = {("model-1", "ollama"): 4096}
    assert _should_skip_oom(_case(context_size=2048), oom, True) is False


def test_skip_oom_respects_flag_disabled() -> None:
    oom: dict[tuple[str, str], int] = {("model-1", "ollama"): 4096}
    assert _should_skip_oom(_case(context_size=8192), oom, False) is False


def test_skip_oom_no_oom_recorded() -> None:
    assert _should_skip_oom(_case(context_size=8192), {}, True) is False


def test_skip_oom_different_model_not_affected() -> None:
    oom: dict[tuple[str, str], int] = {("model-2", "ollama"): 4096}
    assert (
        _should_skip_oom(_case(context_size=8192, model_id="model-1"), oom, True)
        is False
    )


# ---------------------------------------------------------------------------
# _should_skip_concurrency_gate
# ---------------------------------------------------------------------------


def test_skip_gate_blocks_when_no_c1_success() -> None:
    gate: set[tuple[str, str, int]] = set()
    assert _should_skip_concurrency_gate(_case(concurrency=2), gate, True) is True


def test_skip_gate_allows_concurrency_1() -> None:
    gate: set[tuple[str, str, int]] = set()
    assert _should_skip_concurrency_gate(_case(concurrency=1), gate, True) is False


def test_skip_gate_allows_after_c1_success() -> None:
    gate: set[tuple[str, str, int]] = {("model-1", "ollama", 4096)}
    assert (
        _should_skip_concurrency_gate(
            _case(concurrency=2, context_size=4096), gate, True
        )
        is False
    )


def test_skip_gate_respects_flag_disabled() -> None:
    gate: set[tuple[str, str, int]] = set()
    assert _should_skip_concurrency_gate(_case(concurrency=2), gate, False) is False


def test_skip_gate_different_ctx_not_unlocked() -> None:
    gate: set[tuple[str, str, int]] = {("model-1", "ollama", 8192)}
    assert (
        _should_skip_concurrency_gate(
            _case(concurrency=2, context_size=4096), gate, True
        )
        is True
    )


# ---------------------------------------------------------------------------
# _update_adaptive_state
# ---------------------------------------------------------------------------


def test_update_state_oom_sets_threshold() -> None:
    oom_ctx: dict[tuple[str, str], int] = {}
    max_working: dict[tuple[str, str], int] = {}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(_case(context_size=4096), "oom", oom_ctx, max_working, gate)
    assert oom_ctx[("model-1", "ollama")] == 4096


def test_update_state_oom_keeps_minimum() -> None:
    oom_ctx: dict[tuple[str, str], int] = {("model-1", "ollama"): 8192}
    max_working: dict[tuple[str, str], int] = {}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(_case(context_size=4096), "oom", oom_ctx, max_working, gate)
    assert oom_ctx[("model-1", "ollama")] == 4096


def test_update_state_oom_does_not_lower_existing_threshold() -> None:
    oom_ctx: dict[tuple[str, str], int] = {("model-1", "ollama"): 4096}
    max_working: dict[tuple[str, str], int] = {}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(_case(context_size=8192), "oom", oom_ctx, max_working, gate)
    assert oom_ctx[("model-1", "ollama")] == 4096


def test_update_state_ok_updates_max_working_ctx() -> None:
    oom_ctx: dict[tuple[str, str], int] = {}
    max_working: dict[tuple[str, str], int] = {}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(_case(context_size=4096), "ok", oom_ctx, max_working, gate)
    assert max_working[("model-1", "ollama")] == 4096


def test_update_state_ok_keeps_maximum() -> None:
    oom_ctx: dict[tuple[str, str], int] = {}
    max_working: dict[tuple[str, str], int] = {("model-1", "ollama"): 8192}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(_case(context_size=4096), "ok", oom_ctx, max_working, gate)
    assert max_working[("model-1", "ollama")] == 8192


def test_update_state_ok_c1_opens_gate() -> None:
    oom_ctx: dict[tuple[str, str], int] = {}
    max_working: dict[tuple[str, str], int] = {}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(
        _case(context_size=4096, concurrency=1), "ok", oom_ctx, max_working, gate
    )
    assert ("model-1", "ollama", 4096) in gate


def test_update_state_ok_c2_does_not_open_gate() -> None:
    oom_ctx: dict[tuple[str, str], int] = {}
    max_working: dict[tuple[str, str], int] = {}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(
        _case(context_size=4096, concurrency=2), "ok", oom_ctx, max_working, gate
    )
    assert ("model-1", "ollama", 4096) not in gate


def test_update_state_error_does_not_open_gate() -> None:
    oom_ctx: dict[tuple[str, str], int] = {}
    max_working: dict[tuple[str, str], int] = {}
    gate: set[tuple[str, str, int]] = set()
    _update_adaptive_state(
        _case(context_size=4096, concurrency=1), "error", oom_ctx, max_working, gate
    )
    assert ("model-1", "ollama", 4096) not in gate


# ---------------------------------------------------------------------------
# _make_skipped_run
# ---------------------------------------------------------------------------


def test_make_skipped_run_skipped_oom_outcome() -> None:
    case = _case(context_size=8192, concurrency=1)
    run = _make_skipped_run("sweep::case::1", case, "skipped_oom")
    assert run.outcome == "skipped_oom"
    assert run.context_size == 8192
    assert run.repeat_index == 1


def test_make_skipped_run_skipped_concurrency_gate_outcome() -> None:
    case = _case(context_size=4096, concurrency=2)
    run = _make_skipped_run("sweep::case::1", case, "skipped_concurrency_gate")
    assert run.outcome == "skipped_concurrency_gate"
    assert run.concurrency == 2


def test_make_skipped_run_carries_case_fields() -> None:
    case = _case(
        context_size=16384, concurrency=1, model_id="mymodel", backend_id="vllm"
    )
    run = _make_skipped_run("sid::cid::0", case, "skipped_oom")
    assert run.model_id == "mymodel"
    assert run.backend_id == "vllm"
    assert run.tier == "speed"
