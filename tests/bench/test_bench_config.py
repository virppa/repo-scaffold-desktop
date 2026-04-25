"""Tests for BenchConfig.from_toml and BenchConfig.expand_matrix."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.bench.config import BenchCase, BenchConfig

_BENCH_TOML = Path(__file__).parent.parent.parent / "config" / "bench.toml"

_STANDARD_TOML = """
[matrix]
context_sizes = [1024, 4096]
boundary_context_sizes = [8192, 16384]
concurrency_levels = [1, 4]
repeats = 1

[[backends]]
id = "local_a"
enabled = true
base_url = "http://localhost:1/"
api_key = "x"

[[backends]]
id = "cloud_b"
enabled = false
base_url = "http://localhost:2/"
api_key = "y"

[[models]]
id = "model-1"
backend_id = "local_a"

[[models]]
id = "model-2"
backend_id = "local_a"

[[models]]
id = "model-3"
backend_id = "cloud_b"

[[tiers]]
name = "speed"

[[tiers]]
name = "boundary"
"""


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "bench.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_from_toml_loads_real_config() -> None:
    cfg = BenchConfig.from_toml(_BENCH_TOML)
    assert cfg.matrix.context_sizes == [1024, 4096]
    assert len(cfg.backends) >= 1
    assert len(cfg.models) >= 1
    assert len(cfg.tiers) >= 1


def test_bench_case_importable() -> None:
    assert hasattr(BenchCase, "__dataclass_fields__")


def test_expand_matrix_real_and_warmup_counts(tmp_path: Path) -> None:
    cfg = BenchConfig.from_toml(_write_toml(tmp_path, _STANDARD_TOML))
    cases = cfg.expand_matrix()

    real = [c for c in cases if c.repeat_index >= 1]
    warmup = [c for c in cases if c.repeat_index == 0]

    assert len(real) == 16, f"Expected 16 real cases, got {len(real)}"
    assert len(warmup) == 8, f"Expected 8 warmup cases, got {len(warmup)}"


def test_disabled_backend_excluded(tmp_path: Path) -> None:
    toml = """
[matrix]
context_sizes = [1024]
boundary_context_sizes = [8192]
concurrency_levels = [1]
repeats = 1

[[backends]]
id = "off"
enabled = false
base_url = "http://localhost:1/"
api_key = "x"

[[models]]
id = "model-1"
backend_id = "off"

[[tiers]]
name = "speed"
"""
    cfg = BenchConfig.from_toml(_write_toml(tmp_path, toml))
    assert cfg.expand_matrix() == []


def test_invalid_toml_syntax_raises(tmp_path: Path) -> None:
    p = tmp_path / "bench.toml"
    p.write_bytes(b"[matrix\n")  # malformed TOML
    with pytest.raises(Exception):
        BenchConfig.from_toml(p)


def test_invalid_config_structure_raises_validation_error(tmp_path: Path) -> None:
    toml = """
[matrix]
context_sizes = []
boundary_context_sizes = [8192]
concurrency_levels = [1]
"""
    with pytest.raises(ValidationError):
        BenchConfig.from_toml(_write_toml(tmp_path, toml))


def test_boundary_tier_uses_boundary_context_sizes(tmp_path: Path) -> None:
    toml = """
[matrix]
context_sizes = [1024, 4096]
boundary_context_sizes = [8192, 16384]
concurrency_levels = [1]
repeats = 1

[[backends]]
id = "local_a"
enabled = true
base_url = "http://localhost:1/"
api_key = "x"

[[models]]
id = "model-1"
backend_id = "local_a"

[[tiers]]
name = "speed"

[[tiers]]
name = "boundary"
"""
    cfg = BenchConfig.from_toml(_write_toml(tmp_path, toml))
    cases = cfg.expand_matrix()

    boundary_ctx = {c.context_size for c in cases if c.tier == "boundary"}
    speed_ctx = {c.context_size for c in cases if c.tier == "speed"}

    assert boundary_ctx == {8192, 16384}
    assert speed_ctx == {1024, 4096}


def test_warmup_uses_first_concurrency_level(tmp_path: Path) -> None:
    cfg = BenchConfig.from_toml(_write_toml(tmp_path, _STANDARD_TOML))
    warmup = [c for c in cfg.expand_matrix() if c.repeat_index == 0]
    first_concurrency = cfg.matrix.concurrency_levels[0]
    assert all(c.concurrency == first_concurrency for c in warmup)


def test_no_model_on_disabled_backend_in_expanded_cases(tmp_path: Path) -> None:
    cfg = BenchConfig.from_toml(_write_toml(tmp_path, _STANDARD_TOML))
    cases = cfg.expand_matrix()
    backend_ids = {c.backend_id for c in cases}
    assert "cloud_b" not in backend_ids
