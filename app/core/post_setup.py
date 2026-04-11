import subprocess  # nosec B404 — controlled calls with hardcoded command lists, no shell=True
from pathlib import Path


def run_git_init(output_path: Path) -> None:
    """Run `git init` in output_path. Raises RuntimeError on failure."""
    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["git", "init"],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError("git not found on PATH — install git and try again")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git init failed: {stderr}")


def run_precommit_install(output_path: Path) -> None:
    """Run `pre-commit install` in output_path. Raises RuntimeError on failure."""
    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["pre-commit", "install"],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pre-commit not found on PATH — install pre-commit and try again"
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"pre-commit install failed: {stderr}")
