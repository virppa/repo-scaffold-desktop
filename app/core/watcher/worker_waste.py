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


def compute_waste_score(log_path: Path) -> WasteReport:
    """Compute a 0-100 waste score from a worker session log.

    Parses the stream-json log for tool_use entries and counts waste signals:
    redundant_reads, manual_check_runs, redundant_bash, cd_commands, and
    thinking_to_text_ratio.

    Returns a WasteReport with the composite score and per-signal breakdown.
    Returns a zero-score report when the log is missing or unreadable.
    """
    if not log_path.exists():
        logger.debug("Waste score: log not found at %s", log_path)
        return WasteReport(
            score=0,
            breakdown={},
            redundant_reads=0,
            manual_check_runs=0,
            redundant_bash=0,
            cd_commands=0,
            thinking_to_text_ratio=0.0,
        )

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.warning("Waste score: could not read %s", log_path)
        return WasteReport(
            score=0,
            breakdown={},
            redundant_reads=0,
            manual_check_runs=0,
            redundant_bash=0,
            cd_commands=0,
            thinking_to_text_ratio=0.0,
        )

    # --- Collect raw signals ---
    read_counts: dict[str, int] = {}  # file path -> count
    manual_check_runs = 0
    bash_commands: list[str] = []  # for redundancy detection
    cd_commands = 0
    total_thinking_tokens = 0
    total_output_tokens = 0

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

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        for tool_use in _extract_tool_uses(obj):
            name = tool_use.get("name", "")
            inp = tool_use.get("input", {})
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except (json.JSONDecodeError, TypeError):
                    pass

            if name == "Read":
                # Count reads per file path to detect redundant reads.
                # Claude Code's Read tool uses `file_path` as the parameter
                # name (WOR-349); legacy fallbacks kept defensively in case
                # synthetic logs use other keys.
                file_path = (
                    inp.get("file_path", "")
                    or inp.get("path", "")
                    or inp.get("file", "")
                )
                if file_path:
                    read_counts[file_path] = read_counts.get(file_path, 0) + 1

            elif name == "Bash":
                command = ""
                if isinstance(inp, dict):
                    command = str(inp.get("command", ""))
                elif isinstance(inp, str):
                    command = inp

                # Count cd commands.
                if command.strip().startswith("cd "):
                    cd_commands += 1

                # Count manual check runs — only for check tool commands,
                # not for plain cd / ls / etc.
                is_check_tool = False
                for check_cmd in _CHECK_COMMANDS:
                    if check_cmd in command:
                        manual_check_runs += 1
                        is_check_tool = True
                        break

                # Collect bash commands for redundancy detection.
                # Exclude check-tool commands (already counted as
                # manual_check_runs) and cd commands (already counted as
                # cd_commands) to avoid double-counting.
                is_cd = command.strip().startswith("cd ")
                if command and not is_check_tool and not is_cd:
                    bash_commands.append(command)

        # Collect token counts for thinking ratio.
        if obj.get("type") == "assistant":
            msg = obj.get("message") or {}
            usage = msg.get("usage") or {}
            out_tok = usage.get("output_tokens")
            if out_tok is not None:
                total_output_tokens += int(out_tok)

            # Thinking tokens come from content blocks of type "thinking" on
            # assistant messages (WOR-349). The previous code looked for a
            # top-level `message.reasoning` field that does not exist in
            # Claude Code's stream-json format, so total_thinking_tokens was
            # always 0 for real logs.
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "thinking":
                        text = block.get("thinking", "")
                        if isinstance(text, str):
                            total_thinking_tokens += len(text)

    # --- Compute signals ---
    redundant_reads = sum(max(0, c - 1) for c in read_counts.values() if c > 1)

    # Redundant bash: commands that appear more than once (exact match).
    cmd_freq: dict[str, int] = {}
    for cmd in bash_commands:
        cmd_freq[cmd] = cmd_freq.get(cmd, 0) + 1
    redundant_bash = sum(max(0, c - 1) for c in cmd_freq.values() if c > 1)

    # Thinking-to-text ratio: ratio of thinking tokens to output tokens.
    # When output_tokens is 0 or None, ratio is 0.
    if total_output_tokens > 0:
        # Normalize thinking token count (char count) to an approximate token
        # count (roughly 4 chars per token for English text).
        thinking_tokens_approx = total_thinking_tokens / 4.0
        thinking_to_text_ratio = (
            thinking_tokens_approx / total_output_tokens
            if total_output_tokens > 0
            else 0.0
        )
    else:
        thinking_to_text_ratio = 0.0

    # --- Compute composite score ---
    score = 0
    score += min(redundant_reads * 2, 30)
    score += min(manual_check_runs * 3, 25)
    score += min(redundant_bash * 2, 20)
    score += min(cd_commands, 15)
    if thinking_to_text_ratio > 5:
        score += min(int(thinking_to_text_ratio), 10)

    score = min(score, 100)

    # Only include signals that actually contribute (value > 0).
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


def _extract_tool_uses(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all tool_use blocks from a stream-json event.

    Claude Code's stream-json format puts tool_use blocks in
    ``message.content[]`` with ``type == "tool_use"``. A single assistant
    turn can emit multiple tool_use blocks. Returns all of them in order
    (empty list if none).

    Also handles legacy/synthetic forms:
    - direct ``{type: "tool_use", ...}`` event
    - ``{type: "assistant", message: {tool_calls: [{...}]}}`` (legacy field)
    """
    if obj.get("type") == "tool_use":
        return [obj]
    if obj.get("type") != "assistant":
        return []

    msg = obj.get("message") or {}
    found: list[dict[str, Any]] = []

    # Primary path: content blocks (Claude Code stream-json format).
    content = msg.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found.append(block)

    # Legacy fallback: tool_calls list (kept for any synthetic logs).
    tool_calls = msg.get("tool_calls", [])
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict) and tc.get("type") == "tool_use":
                found.append(tc)

    return found
