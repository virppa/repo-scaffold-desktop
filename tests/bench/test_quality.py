"""Tests for the coding quality evaluator."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import scripts.bench.quality as quality_mod
from scripts.bench.quality import evaluate_coding_output


def _make_patch_output(path: str = "solution.py", content: str = "x = 1") -> str:
    return json.dumps({"path": path, "content": content})


def test_returns_false_when_json_parse_fails(tmp_path: Path) -> None:
    result = evaluate_coding_output("not valid json", tmp_path)
    assert result.task_success is False
    assert result.error_message is not None


def test_returns_false_when_patch_missing_keys(tmp_path: Path) -> None:
    result = evaluate_coding_output(json.dumps({"foo": "bar"}), tmp_path)
    assert result.task_success is False
    assert result.error_message is not None


def test_returns_false_when_pytest_fails(tmp_path: Path) -> None:
    failing = MagicMock()
    failing.returncode = 1
    passing = MagicMock()
    passing.returncode = 0
    with patch(
        "scripts.bench.quality.subprocess.run", side_effect=[failing, passing, passing]
    ):
        result = evaluate_coding_output(_make_patch_output(), tmp_path)
    assert result.task_success is False
    assert result.pytest_passed is False
    assert result.ruff_passed is True
    assert result.mypy_passed is True


def test_returns_false_when_ruff_fails(tmp_path: Path) -> None:
    passing = MagicMock()
    passing.returncode = 0
    failing = MagicMock()
    failing.returncode = 1
    with patch(
        "scripts.bench.quality.subprocess.run", side_effect=[passing, failing, passing]
    ):
        result = evaluate_coding_output(_make_patch_output(), tmp_path)
    assert result.task_success is False
    assert result.ruff_passed is False


def test_returns_false_when_mypy_fails(tmp_path: Path) -> None:
    passing = MagicMock()
    passing.returncode = 0
    failing = MagicMock()
    failing.returncode = 1
    with patch(
        "scripts.bench.quality.subprocess.run", side_effect=[passing, passing, failing]
    ):
        result = evaluate_coding_output(_make_patch_output(), tmp_path)
    assert result.task_success is False
    assert result.mypy_passed is False


def test_returns_success_when_all_checks_pass(tmp_path: Path) -> None:
    passing = MagicMock()
    passing.returncode = 0
    with patch("scripts.bench.quality.subprocess.run", return_value=passing):
        result = evaluate_coding_output(_make_patch_output(), tmp_path)
    assert result.task_success is True
    assert result.pytest_passed is True
    assert result.ruff_passed is True
    assert result.mypy_passed is True
    assert result.error_message is None


def test_temp_dir_cleaned_up_after_success(tmp_path: Path) -> None:
    captured: list[str] = []
    orig_mkdtemp = tempfile.mkdtemp

    def capturing_mkdtemp() -> str:
        d = orig_mkdtemp()
        captured.append(d)
        return d

    passing = MagicMock()
    passing.returncode = 0
    with patch.object(quality_mod.tempfile, "mkdtemp", side_effect=capturing_mkdtemp):
        with patch("scripts.bench.quality.subprocess.run", return_value=passing):
            evaluate_coding_output(_make_patch_output(), tmp_path)

    assert captured, "mkdtemp was never called"
    for d in captured:
        assert not Path(d).exists(), f"Temp dir {d} was not cleaned up"


def test_temp_dir_cleaned_up_on_exception(tmp_path: Path) -> None:
    captured: list[str] = []
    orig_mkdtemp = tempfile.mkdtemp

    def capturing_mkdtemp() -> str:
        d = orig_mkdtemp()
        captured.append(d)
        return d

    with patch.object(quality_mod.tempfile, "mkdtemp", side_effect=capturing_mkdtemp):
        with patch(
            "scripts.bench.quality.subprocess.run",
            side_effect=RuntimeError("simulated failure"),
        ):
            result = evaluate_coding_output(_make_patch_output(), tmp_path)

    assert captured, "mkdtemp was never called"
    for d in captured:
        assert not Path(d).exists(), f"Temp dir {d} was not cleaned up"
    assert result.task_success is False
