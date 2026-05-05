"""Smoke tests for scripts.bench.config — TOML parsing, matrix expansion, validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.bench.config import (
    BackendConfig,
    BenchCase,
    BenchConfig,
    MatrixConfig,
    ModelConfig,
)

_MINIMAL_TOML = """
[matrix]
context_sizes = [1024]
boundary_context_sizes = [2048]
concurrency_levels = [1]
repeats = 1

[[backends]]
id = "b1"
enabled = true
base_url = "http://localhost:1/"

[[models]]
id = "m1"
backend_id = "b1"

[[tiers]]
name = "speed"
"""


class TestBenchConfigParsing:
    def test_minimal_toml_loads(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.toml"
        p.write_text(_MINIMAL_TOML, encoding="utf-8")
        cfg = BenchConfig.from_toml(p)
        assert len(cfg.backends) == 1
        assert len(cfg.models) == 1
        assert len(cfg.tiers) == 1

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            BenchConfig.from_toml("/nonexistent/file.toml")

    def test_invalid_toml_syntax_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.toml"
        p.write_bytes(b"[matrix\n")
        with pytest.raises(Exception):
            BenchConfig.from_toml(p)

    def test_extra_forbidden_on_model(self) -> None:
        with pytest.raises(ValidationError, match="extra_forbidden"):
            BackendConfig(id="x", enabled=True, base_url="http://x", extra_field="bad")  # type: ignore[call-arg]


class TestMatrixExpansion:
    def test_warmup_excluded_from_real(self, tmp_path: Path) -> None:
        p = tmp_path / "cfg.toml"
        p.write_text(_MINIMAL_TOML, encoding="utf-8")
        cases = BenchConfig.from_toml(p).expand_matrix()
        real = [c for c in cases if c.repeat_index >= 1]
        warmup = [c for c in cases if c.repeat_index == 0]
        assert len(real) > 0
        assert len(warmup) > 0

    def test_disabled_backend_excluded(self, tmp_path: Path) -> None:
        toml = """
[matrix]
context_sizes = [1024]
boundary_context_sizes = [2048]
concurrency_levels = [1]
repeats = 1

[[backends]]
id = "off"
enabled = false
base_url = "http://localhost:1/"

[[models]]
id = "m1"
backend_id = "off"

[[tiers]]
name = "speed"
"""
        p = tmp_path / "cfg.toml"
        p.write_text(toml, encoding="utf-8")
        cases = BenchConfig.from_toml(p).expand_matrix()
        assert cases == []

    def test_multiple_models_expanded(self, tmp_path: Path) -> None:
        toml = """
[matrix]
context_sizes = [512]
boundary_context_sizes = [1024]
concurrency_levels = [1]
repeats = 1

[[backends]]
id = "b1"
enabled = true
base_url = "http://localhost:1/"

[[models]]
id = "m1"
backend_id = "b1"

[[models]]
id = "m2"
backend_id = "b1"

[[tiers]]
name = "speed"
"""
        p = tmp_path / "cfg.toml"
        p.write_text(toml, encoding="utf-8")
        cases = BenchConfig.from_toml(p).expand_matrix()
        model_ids = {c.model_id for c in cases if c.repeat_index >= 1}
        assert model_ids == {"m1", "m2"}


class TestValidation:
    def test_empty_context_sizes_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            MatrixConfig(
                context_sizes=[],
                boundary_context_sizes=[1],
                concurrency_levels=[1],
            )

    def test_empty_concurrency_levels_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            MatrixConfig(
                context_sizes=[1],
                boundary_context_sizes=[1],
                concurrency_levels=[],
            )

    def test_repeats_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match=">= 1"):
            MatrixConfig(
                context_sizes=[1],
                boundary_context_sizes=[1],
                concurrency_levels=[1],
                repeats=0,
            )

    def test_api_key_defaults_empty(self) -> None:
        cfg = BackendConfig(id="x", enabled=True, base_url="http://x")
        assert cfg.api_key == ""


class TestBenchCase:
    def test_dataclass_fields(self) -> None:
        fields = BenchCase.__dataclass_fields__
        assert "backend_id" in fields
        assert "model_id" in fields
        assert "tier" in fields
        assert "context_size" in fields
        assert "concurrency" in fields
        assert "repeat_index" in fields

    def test_construct_case(self) -> None:
        case = BenchCase(
            backend_id="b",
            model_id="m",
            tier="speed",
            context_size=4096,
            concurrency=2,
            repeat_index=1,
        )
        assert case.model_id == "m"
        assert case.context_size == 4096


class TestModelConfig:
    def test_quant_defaults_to_none(self) -> None:
        m = ModelConfig(id="m", backend_id="b")
        assert m.quant is None

    def test_quant_parsed_from_toml(self, tmp_path: Path) -> None:
        toml = """
[matrix]
context_sizes = [1024]
boundary_context_sizes = [2048]
concurrency_levels = [1]
repeats = 1

[[backends]]
id = "b1"
enabled = true
base_url = "http://localhost:1/"

[[models]]
id = "m1:7b"
backend_id = "b1"
quant = "Q4_K_M"

[[tiers]]
name = "speed"
"""
        p = tmp_path / "cfg.toml"
        p.write_text(toml, encoding="utf-8")
        cfg = BenchConfig.from_toml(p)
        assert cfg.models[0].quant == "Q4_K_M"
