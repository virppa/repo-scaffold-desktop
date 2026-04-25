"""Prefill-shared benchmark: loads a shared fixture document."""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import BenchPrompt

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "prefill_50k.txt"

_SUFFIX_TEMPLATE = (
    "\n\n[Query {index}] Summarise the document above in three bullet points."
)


def make_prefill_shared_prompt(suffix_index: int = 0) -> BenchPrompt:
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"Fixture file not found: {FIXTURE_PATH}. "
            "Generate it first: python scripts/bench/generate_fixtures.py"
            " --generate-fixtures"
        )
    doc = FIXTURE_PATH.read_text(encoding="utf-8")
    suffix = _SUFFIX_TEMPLATE.format(index=suffix_index)
    text = doc + suffix
    prompt_hash = hashlib.sha256(text.encode()).hexdigest()
    return BenchPrompt(
        text=text,
        prompt_hash=prompt_hash,
        task_type="prefill_shared",
        max_tokens=128,
        temperature=0.7,
        seed=None,
    )
