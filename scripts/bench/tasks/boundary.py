"""Boundary benchmark: probes the model at context-window and instruction edges."""

from __future__ import annotations

import hashlib

from . import BenchPrompt

_PASSAGE = "The quick brown fox. " * 500

_TEXT = (
    "You are given a long passage followed by a specific question."
    " Your answer must be derived only from the passage."
    " Do not use outside knowledge.\n\n"
    f"Passage: {_PASSAGE}\n\n"
    "Question: What animal is described as quick and brown?\n"
    "Answer in one sentence."
)


def make_boundary_prompt() -> BenchPrompt:
    prompt_hash = hashlib.sha256(_TEXT.encode()).hexdigest()
    return BenchPrompt(text=_TEXT, prompt_hash=prompt_hash, task_type="boundary")
