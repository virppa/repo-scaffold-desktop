import json
import subprocess
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from app.core.post_setup import (
    create_github_repo,
    fetch_skills,
    run_git_init,
    run_precommit_install,
)


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

    def test_skips_file_on_individual_download_error(self, tmp_path, capsys):
        entries = [
            {"path": ".claude/commands/groom-ticket.md", "type": "blob"},
        ]
        call_count = 0

        def fake_urlopen(req_or_url, timeout=None):  # noqa: ARG001
            nonlocal call_count
            cm = MagicMock()
            cm.__enter__ = lambda s: s
            cm.__exit__ = MagicMock(return_value=False)
            if call_count == 0:
                payload = json.dumps({"tree": entries}).encode()
                cm.read = MagicMock(return_value=payload)
            else:
                raise urllib.error.URLError("timeout")
            call_count += 1
            return cm

        with patch(
            "app.core.post_setup.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            written = fetch_skills(
                tmp_path, "github:virppa/repo-scaffold-skills", "v1.0.0"
            )

        assert written == []
        captured = capsys.readouterr()
        assert "Warning" in captured.out

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


class TestFetchSkillsJinja2:
    def test_renders_template_variables_in_skill_files(self, tmp_path):
        entries = [{"path": ".claude/commands/foo.md", "type": "blob"}]
        template = b"# skill for {{ linear_project }} in {{ repo_name }}"
        with patch(
            "app.core.post_setup.urllib.request.urlopen",
            side_effect=_make_urlopen_mock(entries, template),
        ):
            written = fetch_skills(
                tmp_path,
                "github:virppa/repo-scaffold-skills",
                "v1.0.0",
                context={"linear_project": "MY_PROJECT", "repo_name": "my-repo"},
            )

        assert written == [".claude/commands/foo.md"]
        content = (tmp_path / ".claude/commands/foo.md").read_text(encoding="utf-8")
        assert "MY_PROJECT" in content
        assert "my-repo" in content

    def test_missing_context_key_renders_as_empty_string(self, tmp_path):
        entries = [{"path": ".claude/commands/foo.md", "type": "blob"}]
        template = b"# skill for {{ linear_project }}"
        with patch(
            "app.core.post_setup.urllib.request.urlopen",
            side_effect=_make_urlopen_mock(entries, template),
        ):
            written = fetch_skills(
                tmp_path,
                "github:virppa/repo-scaffold-skills",
                "v1.0.0",
                context={},
            )

        assert written == [".claude/commands/foo.md"]
        content = (tmp_path / ".claude/commands/foo.md").read_text(encoding="utf-8")
        assert "{{ linear_project }}" not in content
        assert content == "# skill for "

    def test_no_context_writes_raw_bytes_unchanged(self, tmp_path):
        entries = [{"path": ".claude/commands/foo.md", "type": "blob"}]
        raw = b"# skill for {{ linear_project }}"
        with patch(
            "app.core.post_setup.urllib.request.urlopen",
            side_effect=_make_urlopen_mock(entries, raw),
        ):
            written = fetch_skills(
                tmp_path,
                "github:virppa/repo-scaffold-skills",
                "v1.0.0",
            )

        assert written == [".claude/commands/foo.md"]
        assert (tmp_path / ".claude/commands/foo.md").read_bytes() == raw


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


class TestCreateGitHubRepo:
    def _make_prefs(self, github_username: str = "testuser") -> MagicMock:
        prefs = MagicMock()
        prefs.github_username = github_username
        return prefs

    def test_creates_repo_and_returns_clone_url(self):
        prefs = self._make_prefs()
        response = MagicMock()
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)
        response.read = MagicMock(
            return_value=b'{"html_url": "https://github.com/testuser/myrepo"}'
        )

        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=response,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake_token"),
        ):
            result = create_github_repo(
                "myrepo", prefs, private=True, description="A test repo"
            )

        assert result == "https://github.com/testuser/myrepo"
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://api.github.com/user/repos"
        assert req.get_method() == "POST"
        body = json.loads(req.data)
        assert body == {
            "name": "myrepo",
            "private": True,
            "description": "A test repo",
            "auto_init": False,
        }

    def test_raises_runtime_error_when_no_token(self):
        prefs = self._make_prefs()
        with patch("app.core.post_setup.get_token", return_value=None):
            with pytest.raises(RuntimeError, match="No GitHub token configured"):
                create_github_repo("myrepo", prefs)

    def test_raises_runtime_error_on_422_conflict(self):
        prefs = self._make_prefs()
        response = MagicMock()
        response.code = 422
        response.read = MagicMock(
            return_value=b'{"message": "Repository already exists"}'
        )
        exc = urllib.error.HTTPError(
            "https://api.github.com/user/repos",
            422,
            "Unprocessable Entity",
            {},
            response,
        )
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=exc,
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake_token"),
        ):
            with pytest.raises(RuntimeError, match="already exists"):
                create_github_repo("myrepo", prefs)

    def test_public_flag_sets_private_false(self):
        prefs = self._make_prefs()
        response = MagicMock()
        response.__enter__ = lambda s: s
        response.__exit__ = MagicMock(return_value=False)
        response.read = MagicMock(
            return_value=b'{"html_url": "https://github.com/testuser/myrepo"}'
        )

        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=response,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake_token"),
        ):
            create_github_repo("myrepo", prefs, private=False)

        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["private"] is False
