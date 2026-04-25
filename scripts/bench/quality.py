"""Coding quality evaluator: applies a patch and runs pytest/ruff/mypy."""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class QualityResult:
    task_success: bool
    pytest_passed: bool
    ruff_passed: bool
    mypy_passed: bool
    error_message: str | None = None


def _apply_patch(patch: dict[str, str], dest: Path) -> None:
    file_path = Path(patch["path"])
    if file_path.is_absolute() or ".." in file_path.parts:
        raise ValueError(f"Unsafe patch path: {patch['path']!r}")
    target = dest / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patch["content"], encoding="utf-8")


def evaluate_coding_output(model_output: str, repo_path: Path) -> QualityResult:
    """Parse model_output as tool-call JSON, apply to a temp repo copy, run checks."""
    temp_dir = tempfile.mkdtemp()
    try:
        patch = json.loads(model_output)
        if not isinstance(patch, dict) or "path" not in patch or "content" not in patch:
            return QualityResult(
                task_success=False,
                pytest_passed=False,
                ruff_passed=False,
                mypy_passed=False,
                error_message=(
                    "model output must be a JSON object with 'path' and 'content' keys"
                ),
            )

        dest = Path(temp_dir)
        shutil.copytree(str(repo_path), str(dest), dirs_exist_ok=True)
        _apply_patch(patch, dest)

        python = sys.executable
        pytest_rc = subprocess.run(  # nosec B603
            [python, "-m", "pytest", "--tb=short", "-q"],
            cwd=dest,
            capture_output=True,
            text=True,
        ).returncode
        ruff_rc = subprocess.run(  # nosec B603
            [python, "-m", "ruff", "check", "."],
            cwd=dest,
            capture_output=True,
            text=True,
        ).returncode
        mypy_rc = subprocess.run(  # nosec B603
            [python, "-m", "mypy", "app/"],
            cwd=dest,
            capture_output=True,
            text=True,
        ).returncode

        pytest_passed = pytest_rc == 0
        ruff_passed = ruff_rc == 0
        mypy_passed = mypy_rc == 0

        return QualityResult(
            task_success=pytest_passed and ruff_passed and mypy_passed,
            pytest_passed=pytest_passed,
            ruff_passed=ruff_passed,
            mypy_passed=mypy_passed,
        )
    except (
        json.JSONDecodeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        return QualityResult(
            task_success=False,
            pytest_passed=False,
            ruff_passed=False,
            mypy_passed=False,
            error_message=str(exc),
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
