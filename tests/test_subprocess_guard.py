"""WOR-426 regression guard: tests must never spawn a real `claude` binary.

Defense-in-depth verification for the autouse fixture in `tests/conftest.py`.
The fixture wraps `subprocess.Popen` to raise `AssertionError` when invoked
with `claude` as argv[0]. This file confirms the guard fires for claude and
does not interfere with legitimate subprocess calls (git, pytest, etc.).
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_guard_rejects_bare_claude_command() -> None:
    """argv=['claude', ...] is rejected with a clear assertion."""
    with pytest.raises(AssertionError, match="real `claude` binary"):
        subprocess.Popen(["claude", "--version"])


def test_guard_rejects_claude_exe_on_windows() -> None:
    """argv=['claude.exe', ...] is also rejected (Windows binary suffix)."""
    with pytest.raises(AssertionError, match="real `claude` binary"):
        subprocess.Popen(["claude.exe", "--version"])


def test_guard_rejects_claude_with_full_path() -> None:
    """An absolute path to claude is rejected — guard inspects argv[0] basename."""
    with pytest.raises(AssertionError, match="real `claude` binary"):
        subprocess.Popen(["/usr/local/bin/claude", "--version"])


def test_guard_allows_git_subprocess() -> None:
    """Legitimate git subprocess calls pass through unaffected."""
    proc = subprocess.Popen(
        ["git", "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert b"git version" in stdout


def test_guard_allows_python_subprocess() -> None:
    """Python subprocess calls (used by the test suite itself) pass through."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('ok')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, _ = proc.communicate(timeout=5)
    assert proc.returncode == 0
    assert stdout.strip() == b"ok"


def test_guard_allows_command_containing_claude_substring() -> None:
    """`git clone claude-something` is allowed — guard checks argv[0] basename only."""
    # Use a fake non-claude binary to verify substring matching doesn't false-positive.
    # This test does NOT actually run the command; it asserts no AssertionError raised
    # at Popen-construction time (the real OS lookup will then fail naturally).
    with pytest.raises((FileNotFoundError, OSError)):
        subprocess.Popen(["definitely-not-a-real-binary-claude-suffix"])
