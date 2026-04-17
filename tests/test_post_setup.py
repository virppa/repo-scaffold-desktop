import json
import subprocess
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from app.core.post_setup import fetch_skills, run_git_init, run_precommit_install


@pytest.fixture()
def repo_dir(tmp_path):
    return tmp_path / "repo"


def _make_urlopen_mock(
    tree_entries: list[dict], file_content: bytes = b"# skill"
) -> MagicMock:
    """Return a mock for urllib.request.urlopen that serves a tree then file blobs."""
    call_count = 0

    def fake_urlopen(req_or_url, timeout=None):  # noqa: ARG001
        nonlocal call_count
        cm = MagicMock()
        if call_count == 0:
            cm.__enter__ = lambda s: s
            cm.__exit__ = MagicMock(return_value=False)
            payload = json.dumps({"tree": tree_entries}).encode()
            cm.read = MagicMock(return_value=payload)
        else:
            cm.__enter__ = lambda s: s
            cm.__exit__ = MagicMock(return_value=False)
            cm.read = MagicMock(return_value=file_content)
        call_count += 1
        return cm

    return fake_urlopen


class TestFetchSkills:
    def test_writes_commands_to_output_path(self, tmp_path):
        entries = [
            {"path": ".claude/commands/groom-ticket.md", "type": "blob"},
            {"path": ".claude/commands/start-ticket.md", "type": "blob"},
            {"path": "README.md", "type": "blob"},  # should be ignored
        ]
        with patch(
            "app.core.post_setup.urllib.request.urlopen",
            side_effect=_make_urlopen_mock(entries, b"# skill content"),
        ):
            written = fetch_skills(
                tmp_path, "github:virppa/repo-scaffold-skills", "v1.0.0"
            )

        assert written == [
            ".claude/commands/groom-ticket.md",
            ".claude/commands/start-ticket.md",
        ]
        assert (tmp_path / ".claude/commands/groom-ticket.md").read_bytes() == (
            b"# skill content"
        )
        assert (tmp_path / ".claude/commands/start-ticket.md").read_bytes() == (
            b"# skill content"
        )
        assert not (tmp_path / "README.md").exists()

    def test_network_error_is_non_fatal(self, tmp_path, capsys):
        with patch(
            "app.core.post_setup.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            written = fetch_skills(
                tmp_path, "github:virppa/repo-scaffold-skills", "v1.0.0"
            )

        assert written == []
        captured = capsys.readouterr()
        assert "Warning" in captured.out

    def test_invalid_source_format_raises_value_error(self, tmp_path):
        with pytest.raises(ValueError, match="Invalid skills_source"):
            fetch_skills(tmp_path, "notgithub:owner/repo", "v1.0.0")

    def test_skips_path_traversal_entries(self, tmp_path, capsys):
        entries = [
            {"path": ".claude/commands/../../../etc/passwd", "type": "blob"},
        ]
        with patch(
            "app.core.post_setup.urllib.request.urlopen",
            side_effect=_make_urlopen_mock(entries),
        ):
            written = fetch_skills(
                tmp_path, "github:virppa/repo-scaffold-skills", "v1.0.0"
            )

        assert written == []
        captured = capsys.readouterr()
        assert "unsafe" in captured.out


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
