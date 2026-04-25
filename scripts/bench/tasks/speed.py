"""Speed benchmark: minimal prompt measuring raw generation latency."""

from __future__ import annotations

import hashlib

from . import BenchPrompt

_TEXT = "Count from 1 to 10."


def make_speed_prompt() -> BenchPrompt:
    prompt_hash = hashlib.sha256(_TEXT.encode()).hexdigest()
    return BenchPrompt(text=_TEXT, prompt_hash=prompt_hash, task_type="speed")
