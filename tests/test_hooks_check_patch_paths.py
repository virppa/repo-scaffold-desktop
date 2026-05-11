"""Tests for the PostToolUse Bash hook on git mv/rename (WOR-379).

The hook in .claude/settings.json is registered as a PostToolUse Bash matcher
that runs ``scripts/check_patch_paths.py``.  Its purpose is to surface stale
patch() module paths to the worker immediately after a rename-shaped command,
so the model can fix them before PR time.

Two layers are tested:

1. **Settings JSON** — the hook entry exists with the correct matcher and
   command.  This is the source-of-truth for *what* the harness runs.

2. **Guard regex** — ``check_patch_paths_post_tool_use.py`` guards on
   ``git mv`` / ``git rename`` patterns so the check script runs *only*
   when a rename happened (not on every Bash invocation).
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "hooks"
    / "check_patch_paths_post_tool_use.py"
)

SETTINGS = Path(__file__).resolve().parent.parent / ".claude" / "settings.json"

# Exact regex pattern used by the hook script to guard on git rename patterns.
_RENAME_RE = re.compile(r"\bgit\s+mv\b|\bgit\s+rename\b")


# ---------------------------------------------------------------------------
# Settings JSON layer
# ---------------------------------------------------------------------------


def test_settings_json_has_post_tool_use_bash_hook() -> None:
    """The PostToolUse Bash entry invokes check_patch_paths_post_tool_use.py."""
    with SETTINGS.open() as f:
        cfg = json.load(f)

    post_hooks = cfg.get("hooks", {}).get("PostToolUse", [])
    bash_hooks = [
        h
        for section in post_hooks
        if section.get("matcher") == "Bash"
        for h in section.get("hooks", [])
    ]
    assert bash_hooks, "No PostToolUse Bash hook registered"
    hook = bash_hooks[0]
    assert hook["type"] == "command"
    assert "check_patch_paths_post_tool_use.py" in hook["command"]


# ---------------------------------------------------------------------------
# Guard regex — the hook's primary logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git mv app/core/old.py app/core/new.py",
        "git mv old_path/ new_path/",
        "git rename app/foo.py",
        "git mv  --force  a  b",
        "  git mv  x  y",
    ],
)
def test_rename_commands_match(cmd: str) -> None:
    """Rename-shaped Bash commands activate the hook."""
    assert _RENAME_RE.search(cmd) is not None, f"{cmd!r} should match"


@pytest.mark.parametrize(
    "cmd",
    [
        "ruff check .",
        "mypy app/",
        "git status",
        "git stash list",
        "git worktree list",
        "git diff --stat",
        "ls -la app/",
        "python -c \"print('hi')\"",
    ],
)
def test_non_rename_commands_dont_match(cmd: str) -> None:
    """Non-rename Bash commands are ignored by the guard."""
    assert _RENAME_RE.search(cmd) is None, f"{cmd!r} should not match"


# ---------------------------------------------------------------------------
# Hook script — integration (doesn't depend on branch state)
# ---------------------------------------------------------------------------


def _invoke_hook(command: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the hook script with a Bash tool-use payload."""
    if command is None:
        payload: dict[str, object] = {"tool_name": "Bash"}
    else:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    return subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_rename_command_exits_clean() -> None:
    """Running the hook on a git mv command does not crash."""
    proc = _invoke_hook("git mv a b")
    assert proc.returncode == 0


def test_non_rename_command_exits_clean() -> None:
    """Running the hook on a non-rename command does not crash."""
    proc = _invoke_hook("ruff check .")
    assert proc.returncode == 0


def test_none_command_exits_clean() -> None:
    """No command key — hook should silently pass (fail-open)."""
    proc = _invoke_hook(None)
    assert proc.returncode == 0


def test_non_bash_tool_exits_clean() -> None:
    """Non-Bash tools (Edit, Write) are ignored by the hook."""
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "app/foo.py", "old_string": "a", "new_string": "b"},
    }
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0


def test_malformed_json_exits_clean() -> None:
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
