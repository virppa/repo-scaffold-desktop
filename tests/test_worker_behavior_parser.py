"""Tests for the WOR-380 per-worker behavior parser
(``_parse_worker_behavior`` in ``watcher_helpers``).

Concurrency-safe sibling to the WOR-370 vLLM /metrics capture: every
field here is derived from the worker's own log file, so the test cases
only need to construct stream-json bytes — no HTTP mocking, no shared
state to reason about.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.watcher.watcher_helpers import (
    WorkerBehavior,
    _parse_worker_behavior,
)


def _write_log(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    """Write a JSONL log file and return its path."""
    log = tmp_path / "worker.log"
    log.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return log


def _assistant_turn(
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    content: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a single assistant-turn JSONL line."""
    msg: dict[str, object] = {"content": content or []}
    if input_tokens is not None or output_tokens is not None:
        usage: dict[str, int] = {}
        if input_tokens is not None:
            usage["input_tokens"] = input_tokens
        if output_tokens is not None:
            usage["output_tokens"] = output_tokens
        msg["usage"] = usage
    return {"type": "assistant", "message": msg}


# ---------------------------------------------------------------------------
# Sentinel returns
# ---------------------------------------------------------------------------


def test_parse_returns_unparseable_when_log_missing(tmp_path: Path) -> None:
    """Missing log file → all None (couldn't determine anything)."""
    result = _parse_worker_behavior(tmp_path / "no_such_log.log")
    assert result.turn_count is None
    assert result.tool_calls_total is None
    assert result.tool_calls_breakdown is None
    assert result.thinking_blocks is None
    assert result.input_tokens_max is None


def test_parse_returns_readable_zero_when_log_empty(tmp_path: Path) -> None:
    """Empty log file → all zero (definitively no activity)."""
    log = tmp_path / "worker.log"
    log.write_text("", encoding="utf-8")
    result = _parse_worker_behavior(log)
    assert result.turn_count == 0
    assert result.tool_calls_total == 0
    assert result.tool_calls_breakdown == {}
    assert result.thinking_blocks == 0
    assert result.thinking_chars_total == 0
    assert result.input_tokens_max is None
    assert result.redundant_reads_count == 0


def test_parse_skips_non_assistant_events(tmp_path: Path) -> None:
    """A log of system / result / user events with no assistant → zero."""
    log = _write_log(
        tmp_path,
        [
            {"type": "system", "subtype": "init"},
            {"type": "user", "message": {"content": "hi"}},
            {"type": "result", "usage": {"input_tokens": 50, "output_tokens": 10}},
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.turn_count == 0


# ---------------------------------------------------------------------------
# Counting basics
# ---------------------------------------------------------------------------


def test_parse_counts_turns_and_input_tokens_trajectory(tmp_path: Path) -> None:
    """First/last/max input_tokens are captured across multiple turns."""
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(input_tokens=49000, output_tokens=10),
            _assistant_turn(input_tokens=51000, output_tokens=20),
            _assistant_turn(input_tokens=55000, output_tokens=30),
            _assistant_turn(input_tokens=53000, output_tokens=40),
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.turn_count == 4
    assert result.input_tokens_first == 49000
    assert result.input_tokens_last == 53000
    assert result.input_tokens_max == 55000


def test_parse_counts_tool_uses_with_breakdown(tmp_path: Path) -> None:
    """tool_use blocks are tallied + bucketed by name."""
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(
                input_tokens=1000,
                content=[
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {"type": "tool_use", "name": "Edit", "input": {}},
                ],
            ),
            _assistant_turn(
                input_tokens=1100,
                content=[
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "b.py"},
                    },
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
            ),
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.tool_calls_total == 5
    assert result.tool_calls_breakdown == {"Read": 2, "Edit": 1, "Bash": 2}


def test_parse_counts_thinking_blocks_and_chars(tmp_path: Path) -> None:
    """`type=thinking` content blocks are counted and their text length summed."""
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(
                input_tokens=100,
                content=[
                    {"type": "thinking", "thinking": "first thought"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
                ],
            ),
            _assistant_turn(
                input_tokens=200,
                content=[{"type": "thinking", "thinking": "second longer thought."}],
            ),
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.thinking_blocks == 2
    # "first thought" (13) + "second longer thought." (22)
    assert result.thinking_chars_total == 13 + 22


def test_parse_thinking_supports_text_field_fallback(tmp_path: Path) -> None:
    """Some emitters use 'text' instead of 'thinking' as the content key."""
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(
                input_tokens=1,
                content=[{"type": "thinking", "text": "via-text-field"}],
            ),
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.thinking_blocks == 1
    assert result.thinking_chars_total == len("via-text-field")


# ---------------------------------------------------------------------------
# Redundant reads (the WOR-355 cap signal)
# ---------------------------------------------------------------------------


def test_parse_redundant_reads_zero_when_each_file_read_at_most_twice(
    tmp_path: Path,
) -> None:
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(
                input_tokens=1,
                content=[
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "b.py"},
                    },
                ],
            ),
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.redundant_reads_count == 0


def test_parse_redundant_reads_counts_files_over_cap(tmp_path: Path) -> None:
    """A file read 3+ times in the session counts as one redundant-read offender."""
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(
                input_tokens=1,
                content=[
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "b.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "b.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "c.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "c.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "c.py"},
                    },
                ],
            ),
        ],
    )
    result = _parse_worker_behavior(log)
    # a.py (4 reads) and c.py (3 reads) — 2 offenders. b.py (2) is at the cap.
    assert result.redundant_reads_count == 2


def test_parse_redundant_reads_normalizes_windows_paths(tmp_path: Path) -> None:
    """Mixed slashes for the same logical path collapse to one counter."""
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(
                input_tokens=1,
                content=[
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "tests\\foo.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "tests/foo.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "tests/foo.py"},
                    },
                ],
            ),
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.redundant_reads_count == 1


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_parse_skips_malformed_json_lines(tmp_path: Path) -> None:
    """A garbage line doesn't crash the parser; valid lines around it parse."""
    log = tmp_path / "worker.log"
    log.write_text(
        "\n".join(
            [
                json.dumps(_assistant_turn(input_tokens=1, output_tokens=1)),
                "not valid json {",
                json.dumps(_assistant_turn(input_tokens=2, output_tokens=2)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = _parse_worker_behavior(log)
    assert result.turn_count == 2


def test_parse_treats_missing_input_tokens_gracefully(tmp_path: Path) -> None:
    """Assistant turns without usage still count as turns; trajectory ignores them."""
    log = _write_log(
        tmp_path,
        [
            _assistant_turn(),
            _assistant_turn(input_tokens=500),
            _assistant_turn(),
        ],
    )
    result = _parse_worker_behavior(log)
    assert result.turn_count == 3
    assert result.input_tokens_first == 500
    assert result.input_tokens_last == 500
    assert result.input_tokens_max == 500


def test_worker_behavior_sentinels_distinguish_unparseable_from_readable() -> None:
    """The two factory sentinels emit semantically different field values."""
    unp = WorkerBehavior.empty_unparseable()
    rdb = WorkerBehavior.empty_readable()
    assert unp.turn_count is None
    assert rdb.turn_count == 0
    assert unp.tool_calls_breakdown is None
    assert rdb.tool_calls_breakdown == {}
    assert unp.input_tokens_max is None
    assert rdb.input_tokens_max is None  # both — readable-empty has no turns
