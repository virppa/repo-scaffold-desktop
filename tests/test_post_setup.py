import subprocess
from unittest.mock import patch

import pytest

from app.core.post_setup import run_git_init, run_precommit_install


@pytest.fixture()
def repo_dir(tmp_path):
    return tmp_path / "repo"


class TestRunGitInit:
    def test_invokes_subprocess_with_correct_args(self, repo_dir):
        with patch("app.core.post_setup.subprocess.run") as mock_run:
            run_git_init(repo_dir)
        mock_run.assert_called_once_with(
            ["git", "init"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

    def test_raises_runtime_error_on_nonzero_exit(self, repo_dir):
        error = subprocess.CalledProcessError(128, "git", stderr=b"not a git repo")
        with patch("app.core.post_setup.subprocess.run", side_effect=error):
            with pytest.raises(RuntimeError, match="git init failed"):
                run_git_init(repo_dir)

    def test_raises_runtime_error_when_git_not_found(self, repo_dir):
        with patch("app.core.post_setup.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="git not found on PATH"):
                run_git_init(repo_dir)


class TestRunPrecommitInstall:
    def test_invokes_subprocess_with_correct_args(self, repo_dir):
        with patch("app.core.post_setup.subprocess.run") as mock_run:
            run_precommit_install(repo_dir)
        mock_run.assert_called_once_with(
            ["pre-commit", "install"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

    def test_raises_runtime_error_on_nonzero_exit(self, repo_dir):
        error = subprocess.CalledProcessError(1, "pre-commit", stderr=b"hook error")
        with patch("app.core.post_setup.subprocess.run", side_effect=error):
            with pytest.raises(RuntimeError, match="pre-commit install failed"):
                run_precommit_install(repo_dir)

    def test_raises_runtime_error_when_precommit_not_found(self, repo_dir):
        with patch("app.core.post_setup.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="pre-commit not found on PATH"):
                run_precommit_install(repo_dir)
