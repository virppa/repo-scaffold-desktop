"""Boundary benchmark: probes the model near the context-window ceiling.

Prompt is sized to 95% of context_size to stress the KV pool and surface OOM
risk or throughput degradation at the limit. Uses the same word-fill approach
as prefill_unshared so the content is semantically similar but pushed higher.
"""

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
_FILL_RATIO = 0.95


def make_boundary_prompt(context_size: int = 131072, seed: int = 99) -> BenchPrompt:
    """Build a prompt sized to 95% of context_size to stress the KV pool ceiling."""
    rng = random.Random(seed)
    target_chars = int(context_size * _FILL_RATIO) * _CHARS_PER_TOKEN
    words: list[str] = []
    total = 0
    while total < target_chars:
        word = rng.choice(_WORDS)
        words.append(word)
        total += len(word) + 1
    passage = " ".join(words)
    text = (
        "You are given a long passage followed by a specific question."
        " Your answer must be derived only from the passage."
        " Do not use outside knowledge.\n\n"
        f"Passage: {passage}\n\n"
        "Question: What is the most frequently repeated word in the passage?\n"
        "Answer in one sentence."
    )
    token_count_estimate = len(text) // _CHARS_PER_TOKEN
    prompt_hash = hashlib.sha256(text.encode()).hexdigest()
    return BenchPrompt(
        text=text,
        prompt_hash=prompt_hash,
        task_type="boundary",
        max_tokens=128,
        temperature=0.7,
        seed=None,
        token_count_estimate=token_count_estimate,
    )
