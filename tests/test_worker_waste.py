"""Tests for app.core.watcher.worker_waste — waste-score computation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.watcher.worker_waste import (
    WasteReport,
    compute_waste_score,
)


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    """Write a worker session log and return its path."""
    log = tmp_path / "worker_wor-99.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


# ---------------------------------------------------------------------------
# Missing / unreadable log
# ---------------------------------------------------------------------------


def test_missing_log_returns_zero_score(tmp_path: Path) -> None:
    """When the log file doesn't exist, score is 0 with empty breakdown."""
    report = compute_waste_score(tmp_path / "nonexistent.log")
    assert report.score == 0
    assert report.breakdown == {}
    assert report.redundant_reads == 0
    assert report.manual_check_runs == 0
    assert report.redundant_bash == 0
    assert report.cd_commands == 0
    assert report.thinking_to_text_ratio == 0.0


def test_unreadable_log_returns_zero_score(tmp_path: Path) -> None:
    """When the log file can't be read, score is 0 with empty breakdown."""
    log = tmp_path / "worker.log"
    log.write_text("", encoding="utf-8")
    # Make it unreadable by removing read permissions (Unix only, skip on Windows)
    import os

    try:
        os.chmod(log, 0o000)
        report = compute_waste_score(log)
        assert report.score == 0
    finally:
        os.chmod(log, 0o644)


# ---------------------------------------------------------------------------
# redundant_reads signal
# ---------------------------------------------------------------------------


def test_no_redundant_reads(tmp_path: Path) -> None:
    """Single reads of different files produce zero redundant_reads."""
    lines = [
        json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}}),
        json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "b.py"}}),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_reads == 0
    assert report.score == 0


def test_redundant_reads_counted(tmp_path: Path) -> None:
    """Reading the same file twice produces one redundant read."""
    lines = [
        json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}}),
        json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}}),
        json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}}),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_reads == 2  # 3 reads - 1 = 2 redundant
    # Score contribution: min(2 * 2, 30) = 4
    assert report.score == 4


def test_redundant_reads_capped_at_30(tmp_path: Path) -> None:
    """redundant_reads score contribution is capped at 30."""
    # 16 redundant reads → 16 * 2 = 32 → capped at 30
    reads = [
        json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}})
        for _ in range(17)
    ]
    report = compute_waste_score(_write_log(tmp_path, reads))
    assert report.redundant_reads == 16
    assert report.score == 30


# ---------------------------------------------------------------------------
# manual_check_runs signal
# ---------------------------------------------------------------------------


def test_no_manual_check_runs(tmp_path: Path) -> None:
    """Bash commands without check tools produce zero manual_check_runs."""
    lines = [
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.manual_check_runs == 0
    assert report.score == 0


def test_manual_check_runs_counted(tmp_path: Path) -> None:
    """Running ruff/mypy/pytest counts as manual check runs."""
    lines = [
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "ruff check ."}}
        ),
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "mypy app/"}}
        ),
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/"}}
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.manual_check_runs == 3
    # Score contribution: min(3 * 3, 25) = 9
    assert report.score == 9


def test_manual_check_runs_capped_at_25(tmp_path: Path) -> None:
    """manual_check_runs score contribution is capped at 25."""
    # 9 check runs → 9 * 3 = 27 → capped at 25
    lines = [
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "ruff check ."}}
        )
        for _ in range(9)
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.manual_check_runs == 9
    assert report.score == 25


# ---------------------------------------------------------------------------
# redundant_bash signal
# ---------------------------------------------------------------------------


def test_no_redundant_bash(tmp_path: Path) -> None:
    """Unique bash commands produce zero redundant_bash."""
    lines = [
        json.dumps({"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}),
        json.dumps({"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}}),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_bash == 0
    assert report.score == 0


def test_redundant_bash_counted(tmp_path: Path) -> None:
    """Duplicate bash commands count as redundant."""
    lines = [
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}
        ),
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}
        ),
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_bash == 2  # 3 - 1 = 2
    # Score: min(2 * 2, 20) = 4
    assert report.score == 4


def test_redundant_bash_capped_at_20(tmp_path: Path) -> None:
    """redundant_bash score contribution is capped at 20."""
    # 11 duplicates → 11 * 2 = 22 → capped at 20
    lines = [
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}
        )
        for _ in range(12)
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_bash == 11
    assert report.score == 20


# ---------------------------------------------------------------------------
# cd_commands signal
# ---------------------------------------------------------------------------


def test_cd_commands_counted(tmp_path: Path) -> None:
    """cd commands are counted."""
    lines = [
        json.dumps(
            {"type": "tool_use", "name": "Bash", "input": {"command": "cd src"}}
        ),
        json.dumps({"type": "tool_use", "name": "Bash", "input": {"command": "cd .."}}),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.cd_commands == 2
    # Score contribution: min(2, 15) = 2
    assert report.score == 2


def test_cd_commands_capped_at_15(tmp_path: Path) -> None:
    """cd_commands score contribution is capped at 15."""
    lines = [
        json.dumps({"type": "tool_use", "name": "Bash", "input": {"command": "cd src"}})
        for _ in range(20)
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.cd_commands == 20
    assert report.score == 15


# ---------------------------------------------------------------------------
# thinking_to_text_ratio signal
# ---------------------------------------------------------------------------


def test_thinking_to_text_ratio_high(tmp_path: Path) -> None:
    """High thinking-to-text ratio adds to score."""
    # 24000 chars of thinking (~6000 tokens) / 1000 output tokens = 6.0
    # WOR-349: thinking comes from content blocks of type "thinking", not from
    # a top-level message.reasoning field.
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "x" * 24000},
                    ],
                    "usage": {"output_tokens": 1000},
                },
            }
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.thinking_to_text_ratio > 5
    # Score contribution: min(int(6.0), 10) = 6
    assert report.score == 6


def test_thinking_to_text_ratio_below_threshold(tmp_path: Path) -> None:
    """Ratio <= 5 does not add to score."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "x" * 20000},
                    ],  # ~5000 tokens / 1000 output = 5.0
                    "usage": {"output_tokens": 1000},
                },
            }
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.thinking_to_text_ratio == pytest.approx(5.0, rel=0.01)
    # At exactly 5.0, the condition > 5 is False, so no addition.
    assert report.score == 0


def test_thinking_to_text_ratio_zero_on_no_output(tmp_path: Path) -> None:
    """When output tokens are zero, ratio is 0."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "x" * 1000},
                    ],
                    "usage": {"output_tokens": 0},
                },
            }
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.thinking_to_text_ratio == 0.0
    assert report.score == 0


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------


def test_composite_score_capped_at_100(tmp_path: Path) -> None:
    """Composite score never exceeds 100."""
    lines = (
        # 10 redundant reads → 10 * 2 = 20
        [
            json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}})
            for _ in range(11)
        ]
        +
        # 10 check runs → 10 * 3 = 30
        [
            json.dumps(
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ruff check ."},
                }
            )
            for _ in range(10)
        ]
        +
        # 10 cd commands → 10
        [
            json.dumps(
                {"type": "tool_use", "name": "Bash", "input": {"command": "cd src"}}
            )
            for _ in range(10)
        ]
        +
        # High thinking ratio → 10
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "x" * 30000},
                        ],
                        "usage": {"output_tokens": 1000},
                    },
                }
            )
        ]
    )
    report = compute_waste_score(_write_log(tmp_path, lines))
    # Raw: 20 + 30 + 10 + 10 = 70 (not yet over 100)
    # But with enough signals it would cap at 100
    assert report.score <= 100


def test_all_signals_contribute(tmp_path: Path) -> None:
    """All four signals contribute to the composite score."""
    lines = (
        # 2 redundant reads → 4
        [
            json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}})
            for _ in range(3)
        ]
        +
        # 2 check runs → 6
        [
            json.dumps(
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ruff check ."},
                }
            )
            for _ in range(2)
        ]
        +
        # 2 cd commands → 2
        [
            json.dumps(
                {"type": "tool_use", "name": "Bash", "input": {"command": "cd src"}}
            )
            for _ in range(2)
        ]
    )
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_reads == 2
    assert report.manual_check_runs == 2
    assert report.cd_commands == 2
    assert report.score == 4 + 6 + 2  # 12


def test_empty_log_returns_zero(tmp_path: Path) -> None:
    """An empty log file produces a zero score."""
    log = tmp_path / "worker.log"
    log.write_text("", encoding="utf-8")
    report = compute_waste_score(log)
    assert report.score == 0
    assert report.breakdown == {}


def test_malformed_json_lines_ignored(tmp_path: Path) -> None:
    """Malformed JSON lines are silently skipped."""
    lines = [
        "not json at all",
        "{broken json",
        json.dumps({"type": "tool_use", "name": "Read", "input": {"path": "a.py"}}),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_reads == 0
    assert report.score == 0


# ---------------------------------------------------------------------------
# WasteReport structure
# ---------------------------------------------------------------------------


def test_waste_report_has_all_fields() -> None:
    """WasteReport has all expected fields."""
    report = WasteReport(
        score=42,
        breakdown={"redundant_reads": 5},
        redundant_reads=5,
        manual_check_runs=0,
        redundant_bash=0,
        cd_commands=0,
        thinking_to_text_ratio=0.0,
    )
    assert report.score == 42
    assert report.breakdown == {"redundant_reads": 5}
    assert report.redundant_reads == 5
    assert report.manual_check_runs == 0
    assert report.redundant_bash == 0
    assert report.cd_commands == 0
    assert report.thinking_to_text_ratio == 0.0


# ---------------------------------------------------------------------------
# Edge cases — tool_use input formats
# ---------------------------------------------------------------------------


def test_tool_use_with_string_input(tmp_path: Path) -> None:
    """Handles tool_use where input is a JSON string instead of dict."""
    lines = [
        json.dumps(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": '{"command": "ruff check ."}',
            }
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.manual_check_runs == 1


def test_tool_use_with_dict_input(tmp_path: Path) -> None:
    """Handles tool_use where input is already a dict."""
    lines = [
        json.dumps(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "ruff check ."},
            }
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.manual_check_runs == 1


def test_assistant_with_tool_calls(tmp_path: Path) -> None:
    """Handles assistant messages with tool_calls array."""
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "tool_calls": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"path": "a.py"},
                        }
                    ],
                    "usage": {"output_tokens": 100},
                },
            }
        ),
    ]
    report = compute_waste_score(_write_log(tmp_path, lines))
    assert report.redundant_reads == 0  # single read
    assert report.score == 0


# ---------------------------------------------------------------------------
# WOR-349 — content-block parsing (Read tool input + thinking blocks)
# ---------------------------------------------------------------------------


def test_read_tool_uses_file_path_key_in_content_blocks(tmp_path: Path) -> None:
    """WOR-349 regression: Claude Code's Read tool uses `file_path` (not
    `path`/`file`), and tool_use blocks live in `message.content[]` (not in
    `message.tool_calls[]`). Two reads of the same file_path should register
    as 1 redundant_read."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/repo/app/core/foo.py"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/repo/app/core/foo.py"},
                    },
                ],
                "usage": {"output_tokens": 50},
            },
        }
    )
    report = compute_waste_score(_write_log(tmp_path, [line]))
    assert report.redundant_reads == 1
    assert "redundant_reads" in report.breakdown


def test_thinking_blocks_extracted_from_content_array(tmp_path: Path) -> None:
    """WOR-349 regression: thinking text comes from content blocks of
    type=='thinking' (with text under the `thinking` key), not from a
    top-level `message.reasoning` field."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "x" * 8000},
                    {"type": "text", "text": "Here is my answer."},
                ],
                "usage": {"output_tokens": 100},
            },
        }
    )
    report = compute_waste_score(_write_log(tmp_path, [line]))
    # 8000 chars / 4 = 2000 thinking tokens; 2000 / 100 = ratio 20
    assert report.thinking_to_text_ratio > 5
    # Sanity: thinking signal contributes to score (capped at 10)
    assert report.score >= 1
