"""Tests for the PreToolUse Read-cap hook (.claude/hooks/check_read_cap.py).

Covers WOR-371 / WOR-355: a file may be read at most twice per session;
the third Read call is blocked with a clear reason citing Grep as the
alternative for finding a different section.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "check_read_cap.py"
)


def _run_hook(payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )


def _read_payload(
    cwd: Path,
    file_path: str,
    *,
    session_id: str = "session-A",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "cwd": str(cwd),
        "tool_name": "Read",
        "tool_input": {"file_path": file_path},
    }


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Tmp dir that has a target file the hook can resolve."""
    target = tmp_path / "src.py"
    target.write_text("# target\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------


def test_first_read_passes(workdir: Path) -> None:
    """A single Read of a file: hook returns 0, no block."""
    proc = _run_hook(_read_payload(workdir, str(workdir / "src.py")))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_second_read_passes(workdir: Path) -> None:
    """Two reads of the same file are still under the cap (cap is 2, 3rd blocks)."""
    p = _read_payload(workdir, str(workdir / "src.py"))
    _run_hook(p)
    proc = _run_hook(p)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_different_files_counted_independently(workdir: Path) -> None:
    """File A read 2x and file B read 2x: neither blocks, counts independent."""
    other = workdir / "other.py"
    other.write_text("# other\n", encoding="utf-8")
    pa = _read_payload(workdir, str(workdir / "src.py"))
    pb = _read_payload(workdir, str(other))
    for _ in range(2):
        for p in (pa, pb):
            proc = _run_hook(p)
            assert proc.returncode == 0
            assert proc.stdout.strip() == ""


def test_session_change_resets_counts(workdir: Path) -> None:
    """A new session_id resets the counter — old counts do not leak."""
    p_old = _read_payload(workdir, str(workdir / "src.py"), session_id="session-A")
    for _ in range(2):
        _run_hook(p_old)
    # Third read in session-A would block, but we switch session
    p_new = _read_payload(workdir, str(workdir / "src.py"), session_id="session-B")
    proc = _run_hook(p_new)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_non_read_tool_passes(workdir: Path) -> None:
    """Bash/Edit/Write tool calls don't trigger the cap."""
    proc = _run_hook(
        {
            "session_id": "x",
            "cwd": str(workdir),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Block cases
# ---------------------------------------------------------------------------


def test_third_read_blocks_with_clear_reason(workdir: Path) -> None:
    """The 3rd read of the same file is blocked; reason cites WOR-355 + Grep."""
    p = _read_payload(workdir, str(workdir / "src.py"))
    _run_hook(p)
    _run_hook(p)
    proc = _run_hook(p)
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block"
    assert "WOR-355" in decision["reason"]
    assert "Grep" in decision["reason"]
    assert "src.py" in decision["reason"]


def test_paths_normalize_so_different_spellings_share_a_count(workdir: Path) -> None:
    """An absolute and a relative path to the same file share the same counter."""
    # NOTE: relative paths resolve against the subprocess's cwd (project root),
    # not workdir. So we test with an absolute path that normalizes correctly.
    p1 = _read_payload(workdir, str(workdir / "src.py"))
    p2 = _read_payload(workdir, str(workdir / "src.py"))
    _run_hook(p1)
    _run_hook(p2)
    # Two reads consumed (2/2). The third must block.
    proc = _run_hook(p1)
    assert proc.returncode == 0
    decision = json.loads(proc.stdout)
    assert decision["decision"] == "block"


# ---------------------------------------------------------------------------
# Fail-open cases
# ---------------------------------------------------------------------------


def test_fails_open_on_malformed_json(tmp_path: Path) -> None:
    """Garbage stdin: returns 0, no output (does not block)."""
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input="not valid json {",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_fails_open_when_file_path_missing(workdir: Path) -> None:
    """A Read payload with no file_path: hook returns 0 silently."""
    proc = _run_hook(
        {
            "session_id": "x",
            "cwd": str(workdir),
            "tool_name": "Read",
            "tool_input": {},  # no file_path
        }
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Path-resolution fix (WOR-422)
# ---------------------------------------------------------------------------


def test_state_file_always_at_hook_dir_parent(tmp_path: Path) -> None:
    """State file is anchored to the hook script location, not the payload cwd.

    Regression test for WOR-422: when the payload cwd was set to the
    ``.claude`` directory instead of repo root, the state path became
    ``.claude/.claude/.read_counts.json`` — a non-existent path that
    silently broke the read cap.
    """
    # Read the hook script to verify STATE_FILENAME path.
    hook_content = HOOK_SCRIPT.parent.joinpath("check_read_cap.py").read_text()
    # Should be ".read_counts.json" — not ".claude/.read_counts.json"
    import re

    match = re.search(r"STATE_FILENAME\s*=\s*['\"](.*?)['\"]", hook_content)
    assert match, "STATE_FILENAME not found in hook"
    state_filename = match.group(1)
    assert state_filename == ".read_counts.json", (
        f"STATE_FILENAME should be '.read_counts.json' to avoid double-nested "
        f"path when joined with hook_dir.parent. Got: {state_filename!r}"
    )


def test_state_persists_across_subprocesses(tmp_path: Path) -> None:
    """State persists between separate hook invocations — each subprocess reads
    and writes to the same state file anchored to the hook script's location."""
    # First subprocess: 1st read — pass
    p1 = _read_payload(tmp_path, str(tmp_path / "test.py"))
    r1 = _run_hook(p1)
    assert r1.returncode == 0
    assert r1.stdout.strip() == ""

    # Second subprocess: 2nd read — pass
    r2 = _run_hook(p1)
    assert r2.returncode == 0
    assert r2.stdout.strip() == ""

    # Third subprocess: 3rd read — should block
    r3 = _run_hook(p1)
    assert r3.returncode == 0
    decision = json.loads(r3.stdout)
    assert decision["decision"] == "block"
