"""Tests for app.core.watcher.watcher_log_parsing."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.watcher.watcher_log_parsing import (
    _parse_worker_api_retries,
    _parse_worker_subagent_spawns,
    _parse_worker_usage,
    format_elapsed,
    format_token_count,
    format_worker_token_count,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "worker_wor-99.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


# ---------------------------------------------------------------------------
# _parse_worker_usage — result-event path
# ---------------------------------------------------------------------------


def test_parse_worker_usage_success(tmp_path: Path) -> None:
    """Result event provides token snapshot; no compact_boundary → 0."""
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
            },
            "context_compactions": 3,
        }
    )
    log = _write_log(tmp_path, ['{"type":"other","x":1}', result_line])
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 1000
    assert output_tok == 200
    assert compactions == 0


def test_parse_worker_usage_no_context_compactions(tmp_path: Path) -> None:
    """Parseable log with no compact_boundary events returns 0."""
    result_line = json.dumps(
        {"type": "result", "usage": {"input_tokens": 500, "output_tokens": 50}}
    )
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 500
    assert output_tok == 50
    assert compactions == 0


def test_parse_worker_usage_missing_log(tmp_path: Path) -> None:
    """Missing log returns None for all three fields."""
    log = tmp_path / "no_such_file.log"
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions is None


def test_parse_worker_usage_no_result_line(tmp_path: Path) -> None:
    """Parseable log without usable usage data returns 0 compactions."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "tool_use", "name": "Bash"}),
            json.dumps({"type": "assistant", "content": "hello"}),
        ],
    )
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions == 0


def test_parse_worker_usage_malformed_json(tmp_path: Path) -> None:
    """Fully unparseable log returns None for all fields."""
    log = tmp_path / "worker.log"
    log.write_text("not json at all\n{broken\n", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions == 0


def test_parse_worker_usage_returns_first_result_line(tmp_path: Path) -> None:
    first = json.dumps(
        {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    second = json.dumps(
        {"type": "result", "usage": {"input_tokens": 999, "output_tokens": 999}}
    )
    log = _write_log(tmp_path, [first, second])
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    # No assistant events — fallback uses the last result event's snapshot.
    assert input_tok == 999
    assert output_tok == 999


def test_parse_worker_usage_empty_file(tmp_path: Path) -> None:
    """Empty file is open-able and parseable → (None, None, 0)."""
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions == 0


# ---------------------------------------------------------------------------
# compact_boundary counting (WOR-357)
# ---------------------------------------------------------------------------


def test_parse_worker_usage_one_compact_boundary(tmp_path: Path) -> None:
    """A single compact_boundary system event yields context_compactions=1."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {
                        "trigger": "auto",
                        "pre_tokens": 135486,
                        "post_tokens": 3348,
                        "duration_ms": 88463,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            ),
        ],
    )
    _, _, compactions, _ = _parse_worker_usage(log)
    assert compactions == 1


def test_parse_worker_usage_multiple_compact_boundaries(tmp_path: Path) -> None:
    """Three compact_boundary events sum to context_compactions=3."""
    boundary = json.dumps(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {"trigger": "auto"},
        }
    )
    log = _write_log(
        tmp_path,
        [
            boundary,
            json.dumps({"type": "assistant", "message": {"id": "a1"}}),
            boundary,
            json.dumps({"type": "assistant", "message": {"id": "a2"}}),
            boundary,
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            ),
        ],
    )
    _, _, compactions, _ = _parse_worker_usage(log)
    assert compactions == 3


def test_parse_worker_usage_compact_duration_summed(tmp_path: Path) -> None:
    """WOR-358: 4th tuple element sums compact_metadata.duration_ms."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"duration_ms": 50000},
                }
            ),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"duration_ms": 38463},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 5},
                }
            ),
        ],
    )
    _, _, compactions, compact_dur = _parse_worker_usage(log)
    assert compactions == 2
    assert compact_dur == 88463


def test_parse_worker_usage_compact_duration_zero_when_no_compactions(
    tmp_path: Path,
) -> None:
    """No compact_boundary events → compact_duration_ms is 0, not None."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    _, _, compactions, compact_dur = _parse_worker_usage(log)
    assert compactions == 0
    assert compact_dur == 0


def test_parse_worker_usage_compact_duration_missing_metadata(tmp_path: Path) -> None:
    """compact_boundary event without duration_ms → counts as compaction but adds 0."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"trigger": "auto"},
                }
            ),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    _, _, compactions, compact_dur = _parse_worker_usage(log)
    assert compactions == 2
    assert compact_dur == 0


def test_parse_worker_usage_other_system_subtypes_ignored(tmp_path: Path) -> None:
    """system events with non-compact_boundary subtypes do not increment."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "system", "subtype": "task_started"}),
            json.dumps({"type": "system", "subtype": "api_retry"}),
            json.dumps({"type": "system", "subtype": "task_notification"}),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 5},
                }
            ),
        ],
    )
    _, _, compactions, _ = _parse_worker_usage(log)
    assert compactions == 0


def test_parse_worker_usage_returns_separate_tokens(tmp_path: Path) -> None:
    """input_tokens and output_tokens are returned separately."""
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 12000, "output_tokens": 800},
        }
    )
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    assert input_tok == 12000
    assert output_tok == 800


def test_parse_worker_usage_missing_input_token_returns_none(
    tmp_path: Path,
) -> None:
    """When input_tokens is absent, all tokens are None."""
    result_line = json.dumps({"type": "result", "usage": {"output_tokens": 500}})
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None


def test_parse_worker_usage_missing_output_token_returns_none(
    tmp_path: Path,
) -> None:
    """When output_tokens is absent, all tokens are None."""
    result_line = json.dumps({"type": "result", "usage": {"input_tokens": 3000}})
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None


# ---------------------------------------------------------------------------
# WOR-357: compact_boundary with assistant usage
# ---------------------------------------------------------------------------


def test_parse_worker_usage_compact_boundary_with_assistant_usage(
    tmp_path: Path,
) -> None:
    """Cumulative assistant-token sum AND compaction count both reported."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "a1",
                        "usage": {"input_tokens": 1000, "output_tokens": 100},
                    },
                }
            ),
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "a2",
                        "usage": {"input_tokens": 500, "output_tokens": 50},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 1500, "output_tokens": 150},
                }
            ),
        ],
    )
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 1500  # 1000 + 500
    assert output_tok == 150  # 100 + 50
    assert compactions == 1


def test_parse_worker_usage_cumulative_output_tokens(tmp_path: Path) -> None:
    """output_tokens summed across every type=assistant event."""
    assistant = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a1",
                    "usage": {"input_tokens": 10000, "output_tokens": 100},
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a2",
                    "usage": {"input_tokens": 10000, "output_tokens": 200},
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a3",
                    "usage": {"input_tokens": 10000, "output_tokens": 300},
                },
            }
        ),
    ]
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 40000, "output_tokens": 707},
            "context_compactions": 5,
        }
    )
    log = tmp_path / "worker.log"
    log.write_text("\n".join(assistant + [result_line]) + "\n", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert output_tok == 600  # 100+200+300
    assert compactions == 0


def test_parse_worker_usage_cumulative_input_tokens(tmp_path: Path) -> None:
    """input_tokens summed across every type=assistant event."""
    assistant = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a1",
                    "usage": {"input_tokens": 8000, "output_tokens": 50},
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a2",
                    "usage": {"input_tokens": 12000, "output_tokens": 60},
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a3",
                    "usage": {"input_tokens": 15000, "output_tokens": 70},
                },
            }
        ),
    ]
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 50000, "output_tokens": 200},
            "context_compactions": 2,
        }
    )
    log = tmp_path / "worker.log"
    log.write_text("\n".join(assistant + [result_line]) + "\n", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 35000  # 8000+12000+15000


def test_parse_worker_usage_mixed_valid_invalid_lines(tmp_path: Path) -> None:
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 300, "output_tokens": 100},
            "context_compactions": 1,
        }
    )
    log = tmp_path / "worker.log"
    log.write_text("garbage line\n" + result_line + "\n", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 300
    assert output_tok == 100
    assert compactions == 0


# ---------------------------------------------------------------------------
# WOR-360 — _parse_worker_api_retries
# ---------------------------------------------------------------------------


def test_parse_worker_api_retries_zero(tmp_path: Path) -> None:
    """Log with no api_retry events returns 0."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_api_retries(log) == 0


def test_parse_worker_api_retries_counts_5(tmp_path: Path) -> None:
    """5 api_retry events returns 5."""
    retry = json.dumps({"type": "system", "subtype": "api_retry"})
    log = _write_log(
        tmp_path,
        [
            retry,
            retry,
            retry,
            retry,
            retry,
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_api_retries(log) == 5


def test_parse_worker_api_retries_other_subtypes_ignored(tmp_path: Path) -> None:
    """Other system subtypes (init, compact_boundary, task_started) ignored."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps({"type": "system", "subtype": "task_started"}),
            json.dumps({"type": "system", "subtype": "task_notification"}),
            json.dumps({"type": "system", "subtype": "api_retry"}),
        ],
    )
    assert _parse_worker_api_retries(log) == 1


def test_parse_worker_api_retries_missing_log(tmp_path: Path) -> None:
    """Missing log returns None (cannot read)."""
    assert _parse_worker_api_retries(tmp_path / "no_such_file.log") is None


# ---------------------------------------------------------------------------
# WOR-364 — _parse_worker_subagent_spawns
# ---------------------------------------------------------------------------


def _task_use(name: str = "Task") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "a1",
                "content": [{"type": "tool_use", "name": name, "input": {}}],
            },
        }
    )


def test_parse_worker_subagent_spawns_zero(tmp_path: Path) -> None:
    """Log with no Task tool_use returns 0."""
    log = _write_log(
        tmp_path,
        [
            _task_use("Read"),
            _task_use("Edit"),
            _task_use("Bash"),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_subagent_spawns(log) == 0


def test_parse_worker_subagent_spawns_counts_3(tmp_path: Path) -> None:
    """3 Task tool_use events returns 3."""
    log = _write_log(
        tmp_path,
        [
            _task_use("Task"),
            _task_use("Read"),
            _task_use("Task"),
            _task_use("Bash"),
            _task_use("Task"),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_subagent_spawns(log) == 3


def test_parse_worker_subagent_spawns_other_tools_ignored(tmp_path: Path) -> None:
    """Read/Edit/Bash/Grep/Write/TodoWrite are not counted."""
    log = _write_log(
        tmp_path,
        [
            _task_use("Read"),
            _task_use("Edit"),
            _task_use("Bash"),
            _task_use("Grep"),
            _task_use("Write"),
            _task_use("TodoWrite"),
        ],
    )
    assert _parse_worker_subagent_spawns(log) == 0


def test_parse_worker_subagent_spawns_missing_log(tmp_path: Path) -> None:
    """Missing log returns None."""
    assert _parse_worker_subagent_spawns(tmp_path / "no_such_file.log") is None


# ---------------------------------------------------------------------------
# format_token_count
# ---------------------------------------------------------------------------


def test_format_token_count_below_threshold() -> None:
    assert format_token_count(0) == "0"
    assert format_token_count(999) == "999"


def test_format_token_count_round_k() -> None:
    assert format_token_count(1000) == "1k"
    assert format_token_count(2000) == "2k"
    assert format_token_count(10000) == "10k"


def test_format_token_count_fractional_k() -> None:
    assert format_token_count(1420) == "1k"  # {1.42:.0f} rounds to 1
    assert format_token_count(5100) == "5k"  # {5.1:.0f} rounds to 5
    assert format_token_count(14999) == "15k"  # {14.999:.0f} rounds to 15


# ---------------------------------------------------------------------------
# format_elapsed
# ---------------------------------------------------------------------------


def test_format_elapsed_zero() -> None:
    assert format_elapsed(0) == "0m00s"


def test_format_elapsed_under_a_minute() -> None:
    assert format_elapsed(30) == "0m30s"
    assert format_elapsed(59) == "0m59s"


def test_format_elapsed_exact_minute() -> None:
    assert format_elapsed(60) == "1m00s"


def test_format_elapsed_minutes_and_seconds() -> None:
    assert format_elapsed(125) == "2m05s"
    assert format_elapsed(3661) == "61m01s"


def test_format_elapsed_large_seconds() -> None:
    """Floors to integer seconds."""
    assert format_elapsed(61.9) == "1m01s"


# ---------------------------------------------------------------------------
# format_worker_token_count
# ---------------------------------------------------------------------------


def test_format_worker_token_count_known(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 1000, "output_tokens": 200},
                }
            ),
        ],
    )
    assert format_worker_token_count(log) == "1k tokens"


def test_format_worker_token_count_unknown(tmp_path: Path) -> None:
    assert format_worker_token_count(tmp_path / "no_such_file.log") == "? tokens"


def test_format_worker_token_count_format_round_k() -> None:
    log = _write_log(
        Path("."),  # placeholder — the log content matters, not the path
        [
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 10000, "output_tokens": 0},
                }
            ),
        ],
    )
    assert format_worker_token_count(log) == "10k tokens"
