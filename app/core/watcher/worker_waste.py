"""Waste-score computation for worker session logs.

Parses the Claude Code stream-json log to detect common waste patterns
(redundant reads, manual check runs, redundant bash, cd commands, thinking
bloat) and returns a 0-100 score.  Formula is identical to the one
calibrated in WOR-277 so the future Streamlit dashboard can reuse it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class WasteReport:
    """Result of a waste-score computation."""

    score: int
    breakdown: dict[str, int]
    # Individual signal counts (for dashboard drill-down).
    redundant_reads: int
    manual_check_runs: int
    redundant_bash: int
    cd_commands: int
    thinking_to_text_ratio: float


_CHECK_COMMANDS = (
    "ruff",
    "mypy",
    "pytest",
    "pre-commit",
    "bandit",
    "semgrep",
    "flake8",
    "black",
    "isort",
    "pylint",
)


def _empty_report() -> "WasteReport":
    """Zero-score sentinel returned when the log is missing/unreadable."""
    return WasteReport(
        score=0,
        breakdown={},
        redundant_reads=0,
        manual_check_runs=0,
        redundant_bash=0,
        cd_commands=0,
        thinking_to_text_ratio=0.0,
    )


def _read_log_lines(log_path: Path) -> list[str] | None:
    """Return log lines or None if the file is missing/unreadable."""
    if not log_path.exists():
        logger.debug("Waste score: log not found at %s", log_path)
        return None
    try:
        return log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("Waste score: could not read %s", log_path)
        return None


def _classify_bash(command: str, bash_buffer: list[str]) -> tuple[int, int]:
    """Classify one Bash command into (manual_check_delta, cd_delta).

    Mutates `bash_buffer` in place when the command counts toward redundancy
    detection (i.e. is not a check tool, not a `cd`, and not empty).
    """
    is_cd = command.strip().startswith("cd ")
    cd_delta = 1 if is_cd else 0

    check_delta = 0
    is_check_tool = False
    for check_cmd in _CHECK_COMMANDS:
        if check_cmd in command:
            check_delta = 1
            is_check_tool = True
            break

    if command and not is_check_tool and not is_cd:
        bash_buffer.append(command)
    return check_delta, cd_delta


def _accumulate_thinking_tokens(obj: dict[str, Any]) -> tuple[int, int]:
    """Return (thinking_chars, output_tokens) contributed by an assistant event."""
    if obj.get("type") != "assistant":
        return 0, 0
    msg = obj.get("message") or {}
    usage = msg.get("usage") or {}
    output = int(usage.get("output_tokens") or 0)

    thinking_chars = 0
    content = msg.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                text = block.get("thinking", "")
                if isinstance(text, str):
                    thinking_chars += len(text)
    return thinking_chars, output


def _parse_tool_input(inp: Any) -> Any:
    """Normalise the `input` field of a tool_use — may arrive as str or dict."""
    if isinstance(inp, str):
        try:
            return json.loads(inp)
        except (json.JSONDecodeError, TypeError):
            return inp
    return inp


def _load_event_or_none(raw_line: str) -> dict[str, Any] | None:
    """Parse one JSONL line into a dict, or return None on blank/invalid lines."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        loaded = json.loads(line)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _accumulate_tool_use(
    tool_use: dict[str, Any],
    read_counts: dict[str, int],
    bash_commands: list[str],
) -> tuple[int, int]:
    """Update read_counts / bash_commands from one tool_use block.

    Returns (manual_check_delta, cd_delta) so the caller can advance its
    aggregate counters without exposing the parsing details.
    """
    name = tool_use.get("name", "")
    inp = _parse_tool_input(tool_use.get("input", {}))
    if name == "Read":
        fp = ""
        if isinstance(inp, dict):
            fp = inp.get("file_path", "") or inp.get("path", "") or inp.get("file", "")
        if fp:
            read_counts[fp] = read_counts.get(fp, 0) + 1
        return 0, 0
    if name == "Bash":
        command = ""
        if isinstance(inp, dict):
            command = str(inp.get("command", ""))
        elif isinstance(inp, str):
            command = inp
        return _classify_bash(command, bash_commands)
    return 0, 0


def _extract_signals(
    lines: list[str],
) -> tuple[dict[str, int], int, list[str], int, int, int]:
    """Walk log lines and aggregate the raw waste signals.

    Returns
    -------
    (read_counts, manual_check_runs, bash_commands, cd_commands,
     total_thinking_chars, total_output_tokens)
    """
    read_counts: dict[str, int] = {}
    manual_check_runs = 0
    bash_commands: list[str] = []
    cd_commands = 0
    total_thinking_chars = 0
    total_output_tokens = 0

    for raw_line in lines:
        obj = _load_event_or_none(raw_line)
        if obj is None:
            continue
        for tool_use in _extract_tool_uses(obj):
            check_delta, cd_delta = _accumulate_tool_use(
                tool_use, read_counts, bash_commands
            )
            manual_check_runs += check_delta
            cd_commands += cd_delta
        chars, output = _accumulate_thinking_tokens(obj)
        total_thinking_chars += chars
        total_output_tokens += output

    return (
        read_counts,
        manual_check_runs,
        bash_commands,
        cd_commands,
        total_thinking_chars,
        total_output_tokens,
    )


def _compute_redundancies(
    read_counts: dict[str, int], bash_commands: list[str]
) -> tuple[int, int]:
    """Return (redundant_reads, redundant_bash) — counts of excess repeats."""
    redundant_reads = sum(max(0, c - 1) for c in read_counts.values() if c > 1)
    cmd_freq: dict[str, int] = {}
    for cmd in bash_commands:
        cmd_freq[cmd] = cmd_freq.get(cmd, 0) + 1
    redundant_bash = sum(max(0, c - 1) for c in cmd_freq.values() if c > 1)
    return redundant_reads, redundant_bash


def _compute_thinking_ratio(thinking_chars: int, output_tokens: int) -> float:
    """Normalise thinking chars to approximate tokens and divide by output."""
    if output_tokens <= 0:
        return 0.0
    return (thinking_chars / 4.0) / output_tokens


def _compute_composite_score(
    redundant_reads: int,
    manual_check_runs: int,
    redundant_bash: int,
    cd_commands: int,
    thinking_to_text_ratio: float,
) -> int:
    """Combine the per-signal counts into a 0-100 waste score."""
    score = 0
    score += min(redundant_reads * 2, 30)
    score += min(manual_check_runs * 3, 25)
    score += min(redundant_bash * 2, 20)
    score += min(cd_commands, 15)
    if thinking_to_text_ratio > 5:
        score += min(int(thinking_to_text_ratio), 10)
    return min(score, 100)


def compute_waste_score(log_path: Path) -> WasteReport:
    """Compute a 0-100 waste score from a worker session log.

    Parses the stream-json log for tool_use entries and counts waste signals:
    redundant_reads, manual_check_runs, redundant_bash, cd_commands, and
    thinking_to_text_ratio.
    """
    lines = _read_log_lines(log_path)
    if lines is None:
        return _empty_report()

    (
        read_counts,
        manual_check_runs,
        bash_commands,
        cd_commands,
        total_thinking_chars,
        total_output_tokens,
    ) = _extract_signals(lines)

    redundant_reads, redundant_bash = _compute_redundancies(read_counts, bash_commands)
    thinking_to_text_ratio = _compute_thinking_ratio(
        total_thinking_chars, total_output_tokens
    )
    score = _compute_composite_score(
        redundant_reads,
        manual_check_runs,
        redundant_bash,
        cd_commands,
        thinking_to_text_ratio,
    )

    breakdown: dict[str, int] = {
        k: int(v)
        for k, v in {
            "redundant_reads": redundant_reads,
            "manual_check_runs": manual_check_runs,
            "redundant_bash": redundant_bash,
            "cd_commands": cd_commands,
            "thinking_to_text_ratio": round(thinking_to_text_ratio, 2),
        }.items()
        if v > 0
    }

    return WasteReport(
        score=score,
        breakdown=breakdown,
        redundant_reads=redundant_reads,
        manual_check_runs=manual_check_runs,
        redundant_bash=redundant_bash,
        cd_commands=cd_commands,
        thinking_to_text_ratio=thinking_to_text_ratio,
    )


def _tool_uses_from_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """Filter a content/tool_calls list down to tool_use dict entries."""
    out: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append(block)
    return out


def _extract_tool_uses(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all tool_use blocks from a stream-json event.

    Claude Code's stream-json format puts tool_use blocks in
    ``message.content[]`` with ``type == "tool_use"``. Also handles legacy
    forms (direct tool_use event, or assistant.tool_calls list).
    """
    if obj.get("type") == "tool_use":
        return [obj]
    if obj.get("type") != "assistant":
        return []
    msg = obj.get("message") or {}
    content = msg.get("content", []) or []
    tool_calls = msg.get("tool_calls", []) or []
    found: list[dict[str, Any]] = []
    if isinstance(content, list):
        found.extend(_tool_uses_from_blocks(content))
    if isinstance(tool_calls, list):
        found.extend(_tool_uses_from_blocks(tool_calls))
    return found
