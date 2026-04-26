"""BackendDriver Protocol and GenerationResult shared by all backend drivers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class GenerationResult:
    """Result of a single generation call to a backend."""

    error: str | None = None
    text: str = ""
    ttft_s: float | None = None
    ttfut_s: float | None = None
    decode_time_s: float | None = None
    prompt_eval_duration_s: float | None = None
    load_duration_s: float | None = None
    cache_state: str | None = None
    raw_prompt_eval_duration_ns: int | None = None
    raw_eval_duration_ns: int | None = None
    raw_load_duration_ns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class BackendDriver(Protocol):
    def is_available(self) -> bool: ...
    def generate(
        self,
        model: str,
        messages: list[dict[str, str]],
        context_size: int,
        max_tokens: int,
        temperature: float,
        seed: int | None,
    ) -> GenerationResult: ...
