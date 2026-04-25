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
    target = 50000
    prompt = make_prefill_unshared_prompt(target=target, seed=42)
    assert prompt.token_count_estimate is not None
    tolerance = target * 0.05
    assert abs(prompt.token_count_estimate - target) <= tolerance


def test_prefill_unshared_deterministic() -> None:
    p1 = make_prefill_unshared_prompt(target=1000, seed=42)
    p2 = make_prefill_unshared_prompt(target=1000, seed=42)
    assert p1.text == p2.text
    assert p1.prompt_hash == p2.prompt_hash


def test_prefill_unshared_different_seeds_differ() -> None:
    p1 = make_prefill_unshared_prompt(target=1000, seed=42)
    p2 = make_prefill_unshared_prompt(target=1000, seed=99)
    assert p1.text != p2.text


def test_coding_prompt_returns_bench_prompt() -> None:
    prompt = make_coding_prompt()
    assert prompt.task_type == "coding"
    assert len(prompt.text) > 0
    assert len(prompt.prompt_hash) == 64


def test_boundary_prompt_returns_bench_prompt() -> None:
    prompt = make_boundary_prompt()
    assert prompt.task_type == "boundary"
    assert len(prompt.text) > 0
    assert len(prompt.prompt_hash) == 64
