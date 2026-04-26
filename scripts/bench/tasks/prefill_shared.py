"""Prefill-shared benchmark: loads a shared fixture document."""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import BenchPrompt

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "prefill_base.txt"

_FILL_RATIO = 0.75
_CHARS_PER_TOKEN = 4

_SUFFIX_TEMPLATE = (
    "\n\n[Query {index}] Summarise the document above in three bullet points."
)


def make_prefill_shared_prompt(
    suffix_index: int = 0, context_size: int = 65536
) -> BenchPrompt:
    """Build a prefill_shared prompt sized to 75% of context_size.

    The shared prefix is the same content for every request at a given
    context_size, enabling KV-cache reuse measurement (vLLM APC).
    suffix_index varies per repeat so each repeat has a distinct prompt hash.
    """
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"Fixture file not found: {FIXTURE_PATH}. "
            "Generate it first: python scripts/bench/run_bench.py --generate-fixtures"
        )
    target_chars = int(context_size * _FILL_RATIO * _CHARS_PER_TOKEN)
    doc = FIXTURE_PATH.read_text(encoding="utf-8")[:target_chars]
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
