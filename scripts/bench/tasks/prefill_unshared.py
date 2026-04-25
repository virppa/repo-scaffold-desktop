"""Prefill-unshared benchmark: generates a fresh random document each run."""

from __future__ import annotations

import hashlib
import random

from . import BenchPrompt

_WORDS = [
    "the",
    "quick",
    "brown",
    "fox",
    "jumps",
    "over",
    "lazy",
    "dog",
    "data",
    "model",
    "system",
    "function",
    "result",
    "value",
    "code",
    "test",
    "output",
    "input",
    "process",
    "task",
    "metric",
    "bench",
    "run",
    "step",
    "call",
    "return",
    "error",
    "success",
    "failure",
    "target",
    "token",
    "prompt",
    "response",
    "context",
    "window",
]

_CHARS_PER_TOKEN = 4


def make_prefill_unshared_prompt(target: int = 50000, seed: int = 42) -> BenchPrompt:
    rng = random.Random(seed)
    target_chars = target * _CHARS_PER_TOKEN
    words: list[str] = []
    total = 0
    while total < target_chars:
        word = rng.choice(_WORDS)
        words.append(word)
        total += len(word) + 1  # +1 for the separating space
    text = " ".join(words)
    token_count_estimate = len(text) // _CHARS_PER_TOKEN
    prompt_hash = hashlib.sha256(text.encode()).hexdigest()
    # seed here is the RNG seed for text generation, not the LLM generation seed.
    # BenchPrompt.seed (None) controls LLM reproducibility separately.
    return BenchPrompt(
        text=text,
        prompt_hash=prompt_hash,
        task_type="prefill_unshared",
        max_tokens=128,
        temperature=0.7,
        seed=None,
        token_count_estimate=token_count_estimate,
    )
