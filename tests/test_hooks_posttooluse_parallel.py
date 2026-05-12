"""Tests for .claude/hooks/posttooluse_parallel.py (WOR-463).

Asserts:
  - All four tools (ruff check, ruff format, mypy, bandit, lint-imports)
    are invoked for a .py file.
  - Phase 2 (mypy/bandit/lint-imports) runs concurrently, not serially:
    wall time is bounded by the slowest single tool, not the sum.
  - Output order is stable (mypy, bandit, lint-imports), not interleaved.
  - Exit code matches ruff's exit code (phase 1 mutator semantics preserved).
  - Non-.py files are skipped silently with exit 0.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Load the hook script as a module without registering it in tests/.
_HOOK_PATH = (
    Path(__file__).parent.parent / ".claude" / "hooks" / "posttooluse_parallel.py"
)
_spec = importlib.util.spec_from_file_location("posttooluse_parallel", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
posttooluse_parallel = importlib.util.module_from_spec(_spec)
sys.modules["posttooluse_parallel"] = posttooluse_parallel
_spec.loader.exec_module(posttooluse_parallel)


def _make_proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_skips_non_python_files(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("hello")
    with patch.object(posttooluse_parallel, "_run") as mock_run:
        rc = posttooluse_parallel.run(str(f))
    assert rc == 0
    mock_run.assert_not_called()


def test_skips_missing_files(tmp_path: Path) -> None:
    f = tmp_path / "missing.py"
    # File does not exist on disk.
    with patch.object(posttooluse_parallel, "_run") as mock_run:
        rc = posttooluse_parallel.run(str(f))
    assert rc == 0
    mock_run.assert_not_called()


def test_invokes_all_four_tools(tmp_path: Path) -> None:
    f = tmp_path / "thing.py"
    f.write_text("x = 1\n")
    with patch.object(
        posttooluse_parallel, "_run", return_value=_make_proc(0)
    ) as mock_run:
        with patch.object(
            posttooluse_parallel.shutil, "which", return_value="/usr/bin/x"
        ):
            rc = posttooluse_parallel.run(str(f))
    assert rc == 0
    invoked_cmds = [call.args[0][0] for call in mock_run.call_args_list]
    # ruff is called twice (check then format); the three readers each once.
    assert invoked_cmds.count("ruff") == 2
    assert "mypy" in invoked_cmds
    assert "bandit" in invoked_cmds
    assert "lint-imports" in invoked_cmds


def test_phase2_runs_concurrently(tmp_path: Path) -> None:
    """Phase 2 wall time should be ~slowest single tool, not the sum."""
    f = tmp_path / "thing.py"
    f.write_text("x = 1\n")

    def slow_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        # Each phase-2 tool sleeps 200ms. Serial = 600ms; parallel ~200ms.
        if cmd[0] in ("mypy", "bandit", "lint-imports"):
            time.sleep(0.2)
        return _make_proc(0)

    with patch.object(posttooluse_parallel, "_run", side_effect=slow_run):
        with patch.object(
            posttooluse_parallel.shutil, "which", return_value="/usr/bin/x"
        ):
            t0 = time.perf_counter()
            posttooluse_parallel.run(str(f))
            elapsed = time.perf_counter() - t0

    # Phase 1 (ruff x2) returns instantly under the mock; phase 2 should take
    # ~200ms parallel + ThreadPoolExecutor overhead. Generous ceiling 500ms
    # (well below the 600ms serial baseline).
    assert elapsed < 0.5, f"Phase 2 not parallel: took {elapsed:.2f}s, expected < 0.5s"


def test_stable_output_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "thing.py"
    f.write_text("x = 1\n")

    def failing_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[0] in ("mypy", "bandit", "lint-imports"):
            return _make_proc(1)  # all three fail
        return _make_proc(0)  # ruff succeeds

    with patch.object(posttooluse_parallel, "_run", side_effect=failing_run):
        with patch.object(
            posttooluse_parallel.shutil, "which", return_value="/usr/bin/x"
        ):
            posttooluse_parallel.run(str(f))

    captured = capsys.readouterr()
    out = captured.out
    mypy_idx = out.find("[mypy]")
    bandit_idx = out.find("[bandit]")
    lint_idx = out.find("[import-linter]")
    assert mypy_idx >= 0 and bandit_idx >= 0 and lint_idx >= 0
    # Stable order: mypy < bandit < lint-imports
    assert mypy_idx < bandit_idx < lint_idx


def test_ruff_failure_propagates_exit_code(tmp_path: Path) -> None:
    f = tmp_path / "thing.py"
    f.write_text("x = 1\n")

    def ruff_fails(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "ruff":
            return _make_proc(1)
        return _make_proc(0)

    with patch.object(posttooluse_parallel, "_run", side_effect=ruff_fails):
        with patch.object(
            posttooluse_parallel.shutil, "which", return_value="/usr/bin/x"
        ):
            rc = posttooluse_parallel.run(str(f))
    assert rc == 1


def test_phase2_failure_does_not_change_exit(tmp_path: Path) -> None:
    """mypy/bandit/lint-imports failures log a message but exit code stays 0."""
    f = tmp_path / "thing.py"
    f.write_text("x = 1\n")

    def phase2_fails(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[0] in ("mypy", "bandit", "lint-imports"):
            return _make_proc(1)
        return _make_proc(0)

    with patch.object(posttooluse_parallel, "_run", side_effect=phase2_fails):
        with patch.object(
            posttooluse_parallel.shutil, "which", return_value="/usr/bin/x"
        ):
            rc = posttooluse_parallel.run(str(f))
    assert rc == 0


def test_missing_tool_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When `which` returns None for bandit, it's skipped without error."""
    f = tmp_path / "thing.py"
    f.write_text("x = 1\n")

    def which_skip_bandit(name: str) -> str | None:
        if name == "bandit":
            return None
        return f"/usr/bin/{name}"

    with patch.object(posttooluse_parallel, "_run", return_value=_make_proc(0)):
        with patch.object(
            posttooluse_parallel.shutil, "which", side_effect=which_skip_bandit
        ):
            rc = posttooluse_parallel.run(str(f))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[bandit]" not in out


def test_main_reads_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "thing.py"
    f.write_text("x = 1\n")
    monkeypatch.setenv("CLAUDE_TOOL_INPUT_FILE_PATH", str(f))
    with patch.object(posttooluse_parallel, "run", return_value=0) as mock_run:
        rc = posttooluse_parallel.main()
    assert rc == 0
    mock_run.assert_called_once_with(str(f))


def test_main_no_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_TOOL_INPUT_FILE_PATH", raising=False)
    with patch.object(posttooluse_parallel, "run") as mock_run:
        rc = posttooluse_parallel.main()
    assert rc == 0
    mock_run.assert_not_called()
