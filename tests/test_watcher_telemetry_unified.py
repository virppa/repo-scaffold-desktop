"""Tests for WOR-466: unified single-pass worker JSONL telemetry parser.

Asserts the unified `_parse_worker_telemetry` walker produces results
identical to running the 5 individual parsers separately. Regression
guard against the 5 walks drifting from the unified walk over time.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.watcher.watcher_helpers import (
    WorkerTelemetry,
    _parse_worker_behavior,
    _parse_worker_telemetry,
)
from app.core.watcher.watcher_log_parsing import (
    _parse_hook_trust_violations,
    _parse_worker_api_retries,
    _parse_worker_subagent_spawns,
    _parse_worker_usage,
)


def _write_log(tmp_path: Path, events: list[dict[str, object]]) -> Path:
    """Write a worker log file with the given JSONL events."""
    log = tmp_path / "worker_wor-1.log"
    with log.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev))
            f.write("\n")
    return log


def test_unified_matches_individual_parsers_on_realistic_log(
    tmp_path: Path,
) -> None:
    """Single walker produces the same output as the 5 separate parsers."""
    events: list[dict[str, object]] = [
        # Two assistant turns with usage + content blocks
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 1000, "output_tokens": 200},
                "content": [
                    {"type": "thinking", "thinking": "thinking some"},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "app/foo.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Task",
                        "input": {"prompt": "spawn subagent"},
                    },
                ],
            },
        },
        {
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": 1500, "output_tokens": 400},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "ruff check ."},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "ls -la"},
                    },
                ],
            },
        },
        # API retry
        {"type": "system", "subtype": "api_retry"},
        # Compact boundary
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {"duration_ms": 250},
        },
        # Result tail (usage fallback won't apply since assistants had usage)
        {"type": "result", "usage": {"input_tokens": 9999, "output_tokens": 9999}},
    ]
    log = _write_log(tmp_path, events)

    # Reference: walk separately
    u_in, u_out, u_comp, u_dur = _parse_worker_usage(log)
    u_spawns = _parse_worker_subagent_spawns(log)
    u_retries = _parse_worker_api_retries(log)
    u_violations = _parse_hook_trust_violations(log)
    u_behavior = _parse_worker_behavior(log)

    # Unified walker
    tel = _parse_worker_telemetry(log)

    assert tel.input_tokens == u_in
    assert tel.output_tokens == u_out
    assert tel.context_compactions == u_comp
    assert tel.compact_duration_ms == u_dur
    assert tel.subagent_spawns == u_spawns
    assert tel.api_retries == u_retries
    assert tel.hook_trust_violations == u_violations
    assert tel.behavior.turn_count == u_behavior.turn_count
    assert tel.behavior.tool_calls_total == u_behavior.tool_calls_total
    assert tel.behavior.tool_calls_breakdown == u_behavior.tool_calls_breakdown
    assert tel.behavior.thinking_blocks == u_behavior.thinking_blocks
    assert tel.behavior.thinking_chars_total == u_behavior.thinking_chars_total
    assert tel.behavior.input_tokens_first == u_behavior.input_tokens_first
    assert tel.behavior.input_tokens_last == u_behavior.input_tokens_last
    assert tel.behavior.input_tokens_max == u_behavior.input_tokens_max
    assert tel.behavior.redundant_reads_count == u_behavior.redundant_reads_count


def test_unified_counts_hook_trust_violation_correctly(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "ruff check ."},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "mypy app/"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "git status"},
                    },
                ],
            },
        },
    ]
    log = _write_log(tmp_path, events)
    tel = _parse_worker_telemetry(log)
    assert tel.hook_trust_violations == 2  # ruff + mypy, not git


def test_unified_subagent_spawns_count(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Task", "input": {}},
                    {"type": "tool_use", "name": "Task", "input": {}},
                ],
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Task", "input": {}}]},
        },
    ]
    log = _write_log(tmp_path, events)
    tel = _parse_worker_telemetry(log)
    assert tel.subagent_spawns == 3


def test_unified_api_retries_count(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {"type": "system", "subtype": "api_retry"},
        {"type": "system", "subtype": "api_retry"},
        {"type": "system", "subtype": "compact_boundary"},  # not a retry
        {"type": "system", "subtype": "api_retry"},
    ]
    log = _write_log(tmp_path, events)
    tel = _parse_worker_telemetry(log)
    assert tel.api_retries == 3


def test_unified_missing_log_returns_empty_unparseable(tmp_path: Path) -> None:
    """Missing file -> FileNotFoundError caught -> empty_unparseable sentinel."""
    missing = tmp_path / "does_not_exist.log"
    tel = _parse_worker_telemetry(missing)
    # All numeric fields None (the OSError catch path).
    assert tel.input_tokens is None
    assert tel.output_tokens is None
    assert tel.subagent_spawns is None
    assert tel.api_retries is None
    assert tel.hook_trust_violations is None
    assert tel.behavior.turn_count is None


def test_unified_empty_log_returns_empty_readable(tmp_path: Path) -> None:
    """Empty (but readable) log -> default counters 0 and empty_readable behavior."""
    log = tmp_path / "empty.log"
    log.write_text("")
    tel = _parse_worker_telemetry(log)
    assert tel.subagent_spawns == 0
    assert tel.api_retries == 0
    assert tel.hook_trust_violations == 0
    assert tel.behavior.turn_count == 0


def test_unified_file_open_failure_returns_empty_unparseable(
    tmp_path: Path, monkeypatch
) -> None:
    """When opening the log raises OSError, returns empty_unparseable()."""
    log = tmp_path / "worker.log"
    log.write_text("ok\n")

    def fake_open(*args: object, **kwargs: object) -> None:
        raise OSError("simulated read error")

    monkeypatch.setattr(Path, "open", fake_open)
    tel = _parse_worker_telemetry(log)
    assert tel.input_tokens is None
    assert tel.subagent_spawns is None
    assert tel.behavior.turn_count is None  # empty_unparseable sentinel


def test_unified_result_fallback_when_no_assistant_usage(tmp_path: Path) -> None:
    """When no assistant turn has usage, fall back to last `result` event."""
    events: list[dict[str, object]] = [
        # Assistant turn with no usage at all (zeros)
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "hi"}],
            },
        },
        {"type": "result", "usage": {"input_tokens": 5000, "output_tokens": 1200}},
    ]
    log = _write_log(tmp_path, events)
    tel = _parse_worker_telemetry(log)
    assert tel.input_tokens == 5000
    assert tel.output_tokens == 1200


def test_worker_telemetry_empty_unparseable() -> None:
    tel = WorkerTelemetry.empty_unparseable()
    assert tel.input_tokens is None
    assert tel.output_tokens is None
    assert tel.subagent_spawns is None
    assert tel.api_retries is None
    assert tel.hook_trust_violations is None
    assert tel.behavior.turn_count is None
