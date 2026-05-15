"""Tests for the PostToolUse parallel-tool nudge hook.

Covers WOR-477: the hook detects consecutive independent single-tool turns
and emits a nudge to parallelise them. Tests cover the trigger path,
no-false-positive suppression (dependent tools), and edge cases (empty
payload, missing tool_output, timeout).
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path

HOOK_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "hooks"
    / "posttooluse_parallel_nudge.py"
)


def _make_payload(
    tool_name: str,
    tool_input: dict,
    tool_output: str = "output",
    *,
    session_id: str = "session-nudge-A",
    state_dir: Path | None = None,
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "cwd": str(Path.cwd()),
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_output": tool_output,
        "message_id": f"msg-{tool_name}",
        "timestamp": time.time(),
        "state_dir": str(state_dir) if state_dir else "",
    }


def _run_hook(
    payload: dict[str, object],
    state_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    # Use payload for state dir — reliable on all platforms
    sd = state_dir or (payload.get("state_dir", "") or None)
    if sd is None:
        sd = Path(tempfile.mkdtemp())
    payload["state_dir"] = str(sd)
    return subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )


def _extract_decision(
    proc: subprocess.CompletedProcess[str],
) -> str | None:
    """Return the nudge text from hook stdout, or None if empty."""
    stdout = proc.stdout.strip()
    if not stdout:
        return None
    return stdout


# ---------------------------------------------------------------------------
# Pass-through: no nudge for < 3 turns
# ---------------------------------------------------------------------------


def test_single_tool_no_nudge(tmp_path: Path) -> None:
    """One tool call: no nudge."""
    proc = _run_hook(
        _make_payload(
            "Read",
            {"file_path": str(tmp_path / "src.py")},
            state_dir=tmp_path,
        )
    )
    assert proc.returncode == 0
    assert _extract_decision(proc) is None


def test_two_independent_tools_no_nudge(
    tmp_path: Path,
) -> None:
    """Two consecutive independent single-tool turns: no nudge."""
    p1 = _make_payload("Bash", {"command": "ls /tmp"}, state_dir=tmp_path)
    p2 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / "file.txt")},
        state_dir=tmp_path,
    )
    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)


# ---------------------------------------------------------------------------
# Trigger path: >= 3 consecutive independent single-tool turns
# ---------------------------------------------------------------------------


def test_nudge_on_three_independent_reads(
    tmp_path: Path,
) -> None:
    """Three consecutive independent Read calls trigger a nudge."""
    p1 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / "a.py")},
        state_dir=tmp_path,
    )
    p2 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / "b.py")},
        state_dir=tmp_path,
    )
    p3 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / "c.py")},
        state_dir=tmp_path,
    )

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    nudge = _extract_decision(r3)
    assert nudge is not None
    assert "nudge" in nudge
    assert "consecutive" in nudge


def test_nudge_on_three_bash_calls(tmp_path: Path) -> None:
    """Three consecutive independent Bash calls trigger a nudge."""
    p1 = _make_payload("Bash", {"command": "echo alpha"}, state_dir=tmp_path)
    p2 = _make_payload("Bash", {"command": "echo beta"}, state_dir=tmp_path)
    p3 = _make_payload("Bash", {"command": "echo gamma"}, state_dir=tmp_path)

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    nudge = _extract_decision(r3)
    assert nudge is not None
    assert nudge is not None
    assert "nudge" in nudge
    assert "consecutive" in nudge


def test_nudge_on_mixed_independent_tools(
    tmp_path: Path,
) -> None:
    """Bash + Read + Bash (all independent) triggers a nudge."""
    p1 = _make_payload("Bash", {"command": "ls /tmp"}, state_dir=tmp_path)
    p2 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / "file.txt")},
        state_dir=tmp_path,
    )
    p3 = _make_payload("Bash", {"command": "echo done"}, state_dir=tmp_path)

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    nudge = _extract_decision(r3)
    assert nudge is not None
    assert nudge is not None
    assert "nudge" in nudge
    assert "consecutive" in nudge


def test_nudge_message_is_readable(tmp_path: Path) -> None:
    """The nudge message mentions consecutive count."""
    p1 = _make_payload("Bash", {"command": "echo 1"}, state_dir=tmp_path)
    p2 = _make_payload("Bash", {"command": "echo 2"}, state_dir=tmp_path)
    p3 = _make_payload("Bash", {"command": "echo 3"}, state_dir=tmp_path)

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    out = r3.stdout.lower()
    assert "consecutive" in out or "parallel" in out


# ---------------------------------------------------------------------------
# Suppression: dependent tools do NOT trigger a nudge
# ---------------------------------------------------------------------------


def test_dependent_bash_then_read_no_nudge(
    tmp_path: Path,
) -> None:
    """Bash creates a file, Read reads it: dependent."""
    file_a = tmp_path / "output.txt"
    file_a.write_text("generated content", encoding="utf-8")

    p1 = _make_payload(
        "Bash",
        {"command": f"echo data > {file_a}"},
        state_dir=tmp_path,
    )
    p2 = _make_payload("Read", {"file_path": str(file_a)}, state_dir=tmp_path)
    p3 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / "other.txt")},
        state_dir=tmp_path,
    )

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    # Bash->Read is dependent (Read references file Bash wrote),
    # so counter resets. The nudge must not fire.
    assert _extract_decision(r3) is None


def test_independent_then_dependent_no_nudge(
    tmp_path: Path,
) -> None:
    """Adding a dependent call resets the independent chain."""
    # Use a path with a dir prefix so regex matches
    shared_name = "src/result.txt"
    (tmp_path / shared_name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / shared_name).write_text("data", encoding="utf-8")

    p1 = _make_payload(
        "Bash",
        {"command": f"cat {tmp_path / shared_name}"},
        state_dir=tmp_path,
    )
    p2 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / "other.txt")},
        state_dir=tmp_path,
    )
    p3 = _make_payload(
        "Read",
        {"file_path": str(tmp_path / shared_name)},
        state_dir=tmp_path,
    )

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    # First two are independent (counter=2).
    # p3 reads a file that p1 read → dependent via full path match.
    assert _extract_decision(r3) is None


def test_read_same_file_is_dependent(tmp_path: Path) -> None:
    """Reading the same file twice: dependent."""
    file_a = tmp_path / "src.py"
    file_a.write_text("# source code", encoding="utf-8")

    p1 = _make_payload("Read", {"file_path": str(file_a)}, state_dir=tmp_path)
    p2 = _make_payload("Read", {"file_path": str(file_a)}, state_dir=tmp_path)
    p3 = _make_payload("Read", {"file_path": str(file_a)}, state_dir=tmp_path)

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    # Each Read depends on the prior (same file).
    # Counter resets, never reaches 3.
    assert _extract_decision(r3) is None


# ---------------------------------------------------------------------------
# Edge cases: empty payload, missing fields, no tool_output
# ---------------------------------------------------------------------------


def test_empty_payload_no_crash() -> None:
    """Empty/invalid payload: hook returns 0, no crash."""
    proc = subprocess.run(  # nosec B603
        [sys.executable, str(HOOK_SCRIPT)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_tool_name() -> None:
    """Payload without tool_name: hook returns 0."""
    proc = _run_hook(
        {
            "session_id": "x",
            "cwd": str(Path.cwd()),
            "tool_input": {},
        },
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_no_tool_output_no_nudge(tmp_path: Path) -> None:
    """A tool call with no tool_output (in-progress)."""
    p1 = _make_payload(
        "Bash",
        {"command": "echo 1"},
        tool_output="",
        state_dir=tmp_path,
    )
    p2 = _make_payload(
        "Bash",
        {"command": "echo 2"},
        tool_output="output2",
        state_dir=tmp_path,
    )
    p3 = _make_payload(
        "Bash",
        {"command": "echo 3"},
        tool_output="output3",
        state_dir=tmp_path,
    )

    _run_hook(p1, state_dir=tmp_path)
    r2 = _run_hook(p2, state_dir=tmp_path)
    r3 = _run_hook(p3, state_dir=tmp_path)

    # p1 has no output so it does not count.
    # Only p2 and p3 count (2 < 3), so no nudge.
    assert _extract_decision(r2) is None
    assert _extract_decision(r3) is None


# ---------------------------------------------------------------------------
# Counter reset on dependency
# ---------------------------------------------------------------------------


def test_dependency_resets_counter(tmp_path: Path) -> None:
    """After a dependency resets the counter, two more calls do not nudge."""
    file_a = tmp_path / "result.txt"
    file_a.write_text("data", encoding="utf-8")

    p1 = _make_payload("Bash", {"command": "echo start"}, state_dir=tmp_path)
    p2 = _make_payload("Read", {"file_path": str(file_a)}, state_dir=tmp_path)
    p3 = _make_payload("Bash", {"command": "echo a"}, state_dir=tmp_path)
    p4 = _make_payload("Bash", {"command": "echo b"}, state_dir=tmp_path)

    _run_hook(p1, state_dir=tmp_path)
    _run_hook(p2, state_dir=tmp_path)
    _run_hook(p3, state_dir=tmp_path)
    r4 = _run_hook(p4, state_dir=tmp_path)

    assert _extract_decision(r4) is None, "Dependency should have reset the counter"


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


def test_different_sessions_separate_state(
    tmp_path: Path,
) -> None:
    """Two sessions count independently."""
    file_b = tmp_path / "other.py"
    file_b.write_text("# other", encoding="utf-8")
    file_c = tmp_path / "third.py"
    file_c.write_text("# third", encoding="utf-8")

    # Session A: two calls (no nudge yet)
    p_a1 = _make_payload(
        "Bash",
        {"command": "echo a"},
        session_id="sess-A",
        state_dir=tmp_path,
    )
    p_a2 = _make_payload(
        "Bash",
        {"command": "echo b"},
        session_id="sess-A",
        state_dir=tmp_path,
    )
    _run_hook(p_a1, state_dir=tmp_path)
    _run_hook(p_a2, state_dir=tmp_path)

    # Session B: three independent calls — should nudge
    p_b1 = _make_payload(
        "Bash",
        {"command": "echo x"},
        session_id="sess-B",
        state_dir=tmp_path,
    )
    p_b2 = _make_payload(
        "Read",
        {"file_path": str(file_b)},
        session_id="sess-B",
        state_dir=tmp_path,
    )
    p_b3 = _make_payload(
        "Read",
        {"file_path": str(file_c)},
        session_id="sess-B",
        state_dir=tmp_path,
    )

    _run_hook(p_b1, state_dir=tmp_path)
    _run_hook(p_b2, state_dir=tmp_path)
    r_b3 = _run_hook(p_b3, state_dir=tmp_path)

    nudge = _extract_decision(r_b3)
    assert nudge is not None, "Session B should have triggered a nudge"
    assert nudge is not None
    assert "nudge" in nudge
    assert "consecutive" in nudge


# ---------------------------------------------------------------------------
# Timeout resets counter
# ---------------------------------------------------------------------------


def test_timeout_resets_counter() -> None:
    """After TIMEOUT_SECONDS, the counter resets."""
    tmp_state = (
        Path(__file__).resolve().parent
        / f".test_nudge_timeout_{int(time.time() * 1000)}"
    )
    tmp_state.mkdir(exist_ok=True)

    state_data = {
        "session_id": "sess-timeout",
        "window_start": time.time() - 600,
        "counter": 5,
        "history": [
            {
                "name": "Bash",
                "input": {"command": "echo 1"},
                "output": "out1",
                "files": [],
                "timestamp": time.time(),
                "message_id": "m1",
            },
            {
                "name": "Bash",
                "input": {"command": "echo 2"},
                "output": "out2",
                "files": [],
                "timestamp": time.time(),
                "message_id": "m2",
            },
            {
                "name": "Bash",
                "input": {"command": "echo 3"},
                "output": "out3",
                "files": [],
                "timestamp": time.time(),
                "message_id": "m3",
            },
        ],
        "last_nudge": 0.0,
        "seen_files": {},
    }
    state_file = tmp_state / ".parallel_nudge_state.json"
    state_file.write_text(json.dumps(state_data), encoding="utf-8")

    p = _make_payload(
        "Bash",
        {"command": "echo 1"},
        session_id="sess-timeout",
        state_dir=tmp_state,
    )
    r = _run_hook(p, state_dir=tmp_state)
    # After timeout, counter resets to 1 (not nudge threshold)
    assert _extract_decision(r) is None

    # Cleanup
    state_file.unlink(missing_ok=True)
    tmp_state.rmdir()
