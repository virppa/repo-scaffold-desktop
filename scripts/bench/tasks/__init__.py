"""Benchmark task prompt factories — shared types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BenchPrompt:
    text: str
    prompt_hash: str
    task_type: str
    max_tokens: int
    temperature: float
    seed: int | None
    token_count_estimate: int | None = None
