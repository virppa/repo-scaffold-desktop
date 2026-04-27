"""Tests for benchmark task prompt factories."""

from __future__ import annotations

import pytest

import scripts.bench.tasks.prefill_shared as prefill_shared_mod
from scripts.bench.tasks.boundary import make_boundary_prompt
from scripts.bench.tasks.coding import make_coding_prompt
from scripts.bench.tasks.prefill_shared import make_prefill_shared_prompt
from scripts.bench.tasks.prefill_unshared import make_prefill_unshared_prompt
from scripts.bench.tasks.speed import make_speed_prompt


def test_speed_prompt_returns_bench_prompt() -> None:
    prompt = make_speed_prompt()
    assert prompt.task_type == "speed"
    assert len(prompt.text) > 0
    assert len(prompt.prompt_hash) == 64  # SHA-256 hex digest
    assert prompt.max_tokens == 256
    assert prompt.temperature == pytest.approx(0.7)
    assert prompt.seed is None


def test_prefill_shared_raises_when_fixture_missing(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefill_shared_mod, "FIXTURE_PATH", tmp_path / "missing.txt")  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        make_prefill_shared_prompt()


def test_prefill_shared_different_suffix_produces_different_hash(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "prefill_50k.txt"  # type: ignore[operator]
    fixture.write_text("Sample document content for testing.", encoding="utf-8")  # type: ignore[union-attr]
    monkeypatch.setattr(prefill_shared_mod, "FIXTURE_PATH", fixture)
    p0 = make_prefill_shared_prompt(suffix_index=0)
    p1 = make_prefill_shared_prompt(suffix_index=1)
    assert p0.prompt_hash != p1.prompt_hash


def test_prefill_unshared_token_count_within_5pct() -> None:
    context_size = 65536
    expected_tokens = int(context_size * 0.75)
    prompt = make_prefill_unshared_prompt(context_size=context_size, seed=42)
    assert prompt.token_count_estimate is not None
    tolerance = expected_tokens * 0.05
    assert abs(prompt.token_count_estimate - expected_tokens) <= tolerance


def test_prefill_unshared_llm_seed_is_none() -> None:
    prompt = make_prefill_unshared_prompt(context_size=1024, seed=42)
    assert prompt.seed is None
    assert prompt.max_tokens == 128


def test_prefill_unshared_deterministic() -> None:
    p1 = make_prefill_unshared_prompt(context_size=4096, seed=42)
    p2 = make_prefill_unshared_prompt(context_size=4096, seed=42)
    assert p1.text == p2.text
    assert p1.prompt_hash == p2.prompt_hash


def test_prefill_unshared_different_seeds_differ() -> None:
    p1 = make_prefill_unshared_prompt(context_size=4096, seed=42)
    p2 = make_prefill_unshared_prompt(context_size=4096, seed=99)
    assert p1.text != p2.text


def test_coding_prompt_returns_bench_prompt() -> None:
    prompt = make_coding_prompt()
    assert prompt.task_type == "coding"
    assert len(prompt.text) > 0
    assert len(prompt.prompt_hash) == 64
    assert prompt.max_tokens == 8192
    assert prompt.temperature == pytest.approx(0.0)
    assert prompt.seed == 42


def test_boundary_prompt_returns_bench_prompt() -> None:
    prompt = make_boundary_prompt()
    assert prompt.task_type == "boundary"
    assert len(prompt.text) > 0
    assert len(prompt.prompt_hash) == 64
    assert prompt.max_tokens == 128
    assert prompt.seed is None
