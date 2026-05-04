"""Tests for the PreToolUse Bash-discipline hook (WOR-376).

Three rules, each tested for the positive (block/warn) and negative
(allow) case, plus fail-open paths.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "hooks"
    / "check_bash_discipline.py"
)


def _run_hook(command: str) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(Path.cwd()),
    }
    return subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Rule 1 — bare `cd <path>` (warn, do not block)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "cd /some/path",
        "  cd /some/path  ",
        "cd ~/foo",
        "cd ../bar",
    ],
)
def test_bare_cd_warns_but_does_not_block(cmd: str) -> None:
    proc = _run_hook(cmd)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""  # No block
    assert "bash-discipline" in proc.stderr
    assert "wasted round-trip" in proc.stderr


@pytest.mark.parametrize(
    "cmd",
    [
        "cd /some/path && ls",
        "cd /some/path; ls",
        "cd /some/path | tee log",
        "cd /some/path > log",
    ],
)
def test_chained_cd_passes_silently(cmd: str) -> None:
    proc = _run_hook(cmd)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert proc.stderr.strip() == ""


# ---------------------------------------------------------------------------
# Rule 2 — heredoc writing source files (block)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "cat > app/foo.py <<EOF\nprint('hi')\nEOF",
        "cat >> docs/README.md << 'EOF'\nHi\nEOF",
        "tee app/config.json <<EOF\n{}\nEOF",
        "cat > pyproject.toml <<EOF\n[tool]\nEOF",
        "cat > .pre-commit-config.yaml <<EOF\nrepos: []\nEOF",
    ],
)
def test_heredoc_to_source_blocks(cmd: str) -> None:
    proc = _run_hook(cmd)
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block"
    assert "heredoc" in decision["reason"]
    assert "Write tool" in decision["reason"]


@pytest.mark.parametrize(
    "cmd",
    [
        "cat app/foo.py",  # plain read
        "echo hi > /tmp/log.txt",  # log file, not source
        "ls > /dev/null",  # noise
        "cat > app/foo.py",  # missing <<
    ],
)
def test_non_heredoc_or_non_source_passes(cmd: str) -> None:
    proc = _run_hook(cmd)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert proc.stderr.strip() == ""


# ---------------------------------------------------------------------------
# Rule 3 — python -c opening files (block)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "python -c \"open('foo.py', 'w').write('x')\"",
        "python3 -c \"with open('foo.py') as f: print(f.read())\"",
        "python3 -c 'data = open(\"x.json\").read()'",
    ],
)
def test_python_dash_c_with_open_blocks(cmd: str) -> None:
    proc = _run_hook(cmd)
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block"
    assert "python -c" in decision["reason"]
    assert "Read" in decision["reason"]
    assert "Edit" in decision["reason"]


@pytest.mark.parametrize(
    "cmd",
    [
        "python -c \"print('hi')\"",  # no open()
        "python3 -c 'import json; print(json.dumps({}))'",  # no open()
        "python script.py",  # not -c
    ],
)
def test_python_without_open_passes(cmd: str) -> None:
    proc = _run_hook(cmd)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Cross-rule and fail-open
# ---------------------------------------------------------------------------


def test_non_bash_tool_passes() -> None:
    """Hook only fires on Bash; an Edit tool call is ignored."""
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"},
    }
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_empty_command_passes() -> None:
    proc = _run_hook("")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_fails_open_on_malformed_json() -> None:
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input="not valid json {",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_block_takes_precedence_over_warn() -> None:
    """A command that matches BOTH 'cd' (warn) and a heredoc (block) blocks.

    This tests the rule precedence: more dangerous patterns checked first.
    Even though `cd` requires a *bare* command and a heredoc-containing
    string is not bare, this asserts the explicit ordering.
    """
    cmd = "cd /tmp && cat > app/foo.py <<EOF\nx\nEOF"
    proc = _run_hook(cmd)
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block"
    assert "heredoc" in decision["reason"]
