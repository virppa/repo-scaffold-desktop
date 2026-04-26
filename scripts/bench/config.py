"""Benchmark config loader and matrix expander."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, field_validator


@dataclass
class BenchCase:
    """One probe point in the benchmark matrix."""

    backend_id: str
    model_id: str
    tier: str
    context_size: int
    concurrency: int
    repeat_index: int  # 0 = warmup, >=1 = real


class MatrixConfig(BaseModel):
    model_config = {"extra": "forbid"}

    context_sizes: list[int]
    boundary_context_sizes: list[int]
    concurrency_levels: list[int]
    repeats: int = 1
    skip_oom_larger_ctx: bool = True
    require_single_concurrency_first: bool = True

    @field_validator("context_sizes", "boundary_context_sizes", "concurrency_levels")
    @classmethod
    def non_empty_list(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("must be non-empty")
        return v

    @field_validator("repeats")
    @classmethod
    def positive_repeats(cls, v: int) -> int:
        if v < 1:
            raise ValueError("repeats must be >= 1")
        return v


class BackendConfig(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    enabled: bool = True
    base_url: str
    api_key: str = ""


class ModelConfig(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    backend_id: str
    quant: str | None = None


class TierConfig(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    context_sizes: list[int] | None = None


class BenchConfig(BaseModel):
    """Top-level benchmark configuration."""

    model_config = {"extra": "forbid"}

    matrix: MatrixConfig
    backends: list[BackendConfig]
    models: list[ModelConfig]
    tiers: list[TierConfig]

    @classmethod
    def from_toml(cls, path: Path | str) -> "BenchConfig":
        """Load and validate config from a TOML file.

        Raises FileNotFoundError if the file is missing.
        Raises pydantic.ValidationError if the config is structurally invalid.
        Raises tomllib.TOMLDecodeError if the file is not valid TOML.
        """
        resolved = Path(path)
        raw = resolved.read_bytes()
        data = tomllib.loads(raw.decode())
        return cls.model_validate(data)

    def expand_matrix(self) -> list[BenchCase]:
        """Expand the config into a flat list of BenchCase instances.

        Disabled backends are excluded. Boundary tier uses boundary_context_sizes;
        all other tiers use context_sizes. Each (model, tier, context_size) produces
        one warmup case (repeat_index=0) at the first concurrency level, followed by
        real cases (repeat_index>=1) across all concurrency levels and repeats.
        """
        enabled_backends = {b.id for b in self.backends if b.enabled}
        enabled_models = [m for m in self.models if m.backend_id in enabled_backends]

        cases: list[BenchCase] = []

        for model in enabled_models:
            for tier in self.tiers:
                if tier.name == "boundary":
                    ctx_sizes = self.matrix.boundary_context_sizes
                elif tier.context_sizes is not None:
                    ctx_sizes = tier.context_sizes
                else:
                    ctx_sizes = self.matrix.context_sizes

                for ctx_size in ctx_sizes:
                    cases.append(
                        BenchCase(
                            backend_id=model.backend_id,
                            model_id=model.id,
                            tier=tier.name,
                            context_size=ctx_size,
                            concurrency=self.matrix.concurrency_levels[0],
                            repeat_index=0,
                        )
                    )

                    for concurrency in self.matrix.concurrency_levels:
                        for repeat in range(1, self.matrix.repeats + 1):
                            cases.append(
                                BenchCase(
                                    backend_id=model.backend_id,
                                    model_id=model.id,
                                    tier=tier.name,
                                    context_size=ctx_size,
                                    concurrency=concurrency,
                                    repeat_index=repeat,
                                )
                            )

        return cases
