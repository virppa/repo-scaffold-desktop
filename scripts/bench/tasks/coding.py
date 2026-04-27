"""Coding benchmark: asks the model to implement a small utility function."""

from __future__ import annotations

import hashlib

from . import BenchPrompt

_TEXT = (
    "Implement a Python function `flatten(nested)` that recursively flattens"
    " a nested list of arbitrary depth into a single flat list.\n\n"
    "Requirements:\n"
    "- Handle lists nested to any depth.\n"
    "- Leave non-list values (int, str, float, etc.) as-is.\n"
    "- Return a new flat list; do not mutate the input.\n\n"
    "Provide only the function definition — no example usage or imports.\n"
    'Return your answer as a JSON object: {"path": "solution.py",'
    ' "content": "<code>"}'
)


def make_coding_prompt() -> BenchPrompt:
    prompt_hash = hashlib.sha256(_TEXT.encode()).hexdigest()
    return BenchPrompt(
        text=_TEXT,
        prompt_hash=prompt_hash,
        task_type="coding",
        max_tokens=8192,
        temperature=0.0,
        seed=42,
    )
