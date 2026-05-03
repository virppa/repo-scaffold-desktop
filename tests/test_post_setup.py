import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from app.core.post_setup import (
    configure_github_repo,
    create_github_repo,
    delete_github_repo,
    fetch_skills,
    run_git_init,
    run_initial_push,
    run_precommit_install,
)
from app.core.user_prefs import UserPreferences


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
    def test_creates_repo_and_returns_clone_url(self):
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
                "myrepo", private=True, description="A test repo"
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
        with patch("app.core.post_setup.get_token", return_value=None):
            with pytest.raises(RuntimeError, match="No GitHub token configured"):
                create_github_repo("myrepo")

    def test_raises_runtime_error_on_422_conflict(self):
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
                create_github_repo("myrepo")

    def test_public_flag_sets_private_false(self):
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
            create_github_repo("myrepo", private=False)

        body = json.loads(mock_urlopen.call_args[0][0].data)
        assert body["private"] is False


class TestRunInitialPush:
    URL = "https://github.com/user/repo"

    def _expected_calls(self, repo_dir, with_auth=False):
        kw = {"cwd": repo_dir, "check": True, "capture_output": True}
        base = [
            call(["git", "add", "."], **kw),
        ]
        if with_auth:
            base.append(
                call(
                    [
                        "git",
                        "-c",
                        "user.name=Alice",
                        "-c",
                        "user.email=alice@example.com",
                        "commit",
                        "-m",
                        "Initial scaffold",
                    ],
                    **kw,
                )
            )
        else:
            base.append(
                call(
                    ["git", "commit", "-m", "Initial scaffold"],
                    **kw,
                )
            )
        return base + [
            call(
                ["git", "remote", "add", "origin", self.URL],
                **kw,
            ),
            call(
                ["git", "branch", "-M", "main"],
                **kw,
            ),
            call(
                ["git", "push", "-u", "origin", "main"],
                **kw,
            ),
        ]

    def test_executes_full_command_sequence(self, repo_dir):
        with patch("app.core.post_setup.subprocess.run") as mock_run:
            run_initial_push(repo_dir, self.URL, UserPreferences())

        expected = self._expected_calls(repo_dir, with_auth=False)
        assert mock_run.call_args_list == expected

    def test_includes_author_name_and_email_when_both_set(self, repo_dir):
        prefs = UserPreferences(author_name="Alice", author_email="alice@example.com")
        with patch("app.core.post_setup.subprocess.run") as mock_run:
            run_initial_push(repo_dir, self.URL, prefs)

        expected = self._expected_calls(repo_dir, with_auth=True)
        assert mock_run.call_args_list == expected

    def test_falls_back_to_git_default_when_only_name_set(self, repo_dir):
        prefs = UserPreferences(author_name="Alice")
        with patch("app.core.post_setup.subprocess.run") as mock_run:
            run_initial_push(repo_dir, self.URL, prefs)

        expected = self._expected_calls(repo_dir, with_auth=False)
        assert mock_run.call_args_list == expected

    def test_falls_back_to_git_default_when_only_email_set(self, repo_dir):
        prefs = UserPreferences(author_email="alice@example.com")
        with patch("app.core.post_setup.subprocess.run") as mock_run:
            run_initial_push(repo_dir, self.URL, prefs)

        expected = self._expected_calls(repo_dir, with_auth=False)
        assert mock_run.call_args_list == expected

    def test_raises_runtime_error_on_subprocess_failure(self):
        err = subprocess.CalledProcessError(
            1, "git", stderr=b"fatal: not a git repository"
        )
        with patch("app.core.post_setup.subprocess.run", side_effect=err):
            with pytest.raises(RuntimeError, match="not a git repository"):
                run_initial_push(Path("/tmp"), self.URL, UserPreferences())

    def test_runtime_error_includes_stderr(self):
        err = subprocess.CalledProcessError(
            128, "git", stderr=b"fatal: 'origin' already exists"
        )
        with patch("app.core.post_setup.subprocess.run", side_effect=err):
            with pytest.raises(RuntimeError, match="'origin' already exists"):
                run_initial_push(Path("/tmp"), self.URL, UserPreferences())


class TestConfigureGithubRepo:
    def _make_response(self, status: int = 200, body: dict | None = None) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = MagicMock(return_value=json.dumps(body or {}).encode())
        type(cm).status = status
        return cm

    def _make_http_error(
        self, code: int, body: str, msg: str = ""
    ) -> urllib.error.HTTPError:
        fp = MagicMock()
        fp.read = MagicMock(return_value=body.encode())
        if not msg:
            msg = body
        return urllib.error.HTTPError(
            "https://api.github.com/repos/test/repo",
            code,
            msg,
            {},
            fp,
        )

    def _make_urlopen_side_effect(self, responses: list) -> MagicMock:
        idx = [0]

        def side_effect(req_or_url, timeout=None):  # noqa: ARG001
            if idx[0] < len(responses):
                resp = responses[idx[0]]
                idx[0] += 1
                return resp
            raise RuntimeError("Unexpected call")

        return side_effect

    def test_sets_topics_from_preset(self):
        resp = self._make_response(200, {"repository": {"topics": ["python"]}})
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            configure_github_repo("test/repo", "python_basic", False)

        assert mock_urlopen.call_count == 2  # topics + PATCH
        topics_req = mock_urlopen.call_args_list[0][0][0]
        assert topics_req.method == "PUT"
        assert "topics" in topics_req.full_url
        body = json.loads(topics_req.data)
        assert body == {"names": ["python"]}

    def test_topics_for_python_desktop_preset(self):
        resp = self._make_response(200)
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            configure_github_repo("test/repo", "python_desktop", False)

        topics_req = mock_urlopen.call_args_list[0][0][0]
        body = json.loads(topics_req.data)
        assert body == {"names": ["python", "pyside6", "desktop"]}

    def test_topics_for_full_agentic_preset(self):
        resp = self._make_response(200)
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            configure_github_repo("test/repo", "full_agentic", False)

        topics_req = mock_urlopen.call_args_list[0][0][0]
        body = json.loads(topics_req.data)
        assert sorted(body["names"]) == sorted(
            ["python", "claude", "agentic", "linear"]
        )

    def test_disables_wiki_and_projects(self):
        resp = self._make_response(200)
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            configure_github_repo("test/repo", "python_basic", False)

        assert mock_urlopen.call_count == 2  # topics + PATCH
        patch_req = mock_urlopen.call_args_list[1][0][0]
        assert patch_req.method == "PATCH"
        assert "/repos/test/repo" in patch_req.full_url
        assert "/topics" not in patch_req.full_url
        body = json.loads(patch_req.data)
        assert body == {"has_wiki": False, "has_projects": False}

    def test_sets_branch_protection_when_include_ci_true(self):
        resp = self._make_response(200)
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            configure_github_repo("test/repo", "python_basic", include_ci=True)

        assert mock_urlopen.call_count == 3
        protection_req = mock_urlopen.call_args_list[2][0][0]
        assert protection_req.method == "PUT"
        assert "/branches/main/protection" in protection_req.full_url
        body = json.loads(protection_req.data)
        review = body["required_pull_request_reviews"]
        assert review["required_approving_review_count"] == 1
        assert review["require_code_owner_reviews"] is True
        assert body["required_status_checks"]["strict"] is True

    def test_skips_branch_protection_when_include_ci_false(self):
        resp = self._make_response(200)
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            configure_github_repo("test/repo", "python_basic", include_ci=False)

        assert mock_urlopen.call_count == 2  # topics + PATCH only
        protection_url = (
            "https://api.github.com/repos/test/repo/branches/main/protection"
        )
        for call_item in mock_urlopen.call_args_list:
            req = call_item[0][0]
            assert protection_url not in req.full_url

    def test_invalid_full_name_format_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid repo_full_name"):
            configure_github_repo("badformat", "python_basic", False)

    def test_invalid_full_name_multiple_slashes_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid repo_full_name"):
            configure_github_repo("test/repo/extra", "python_basic", False)

    def test_no_slash_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid repo_full_name"):
            configure_github_repo("test", "python_basic", False)

    def test_invalid_full_name_before_token_check(self):
        """Format validation must happen BEFORE get_token() is called."""
        with patch(
            "app.core.post_setup.get_token", return_value="ghp_fake"
        ) as mock_get_token:
            with pytest.raises(ValueError, match="Invalid repo_full_name"):
                configure_github_repo("badformat", "python_basic", False)

        # get_token must NOT have been called for invalid full_name
        mock_get_token.assert_not_called()

    def test_topics_raises_runtime_error_on_4xx(self):
        exc = self._make_http_error(403, "forbidden")
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=exc,
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            with pytest.raises(RuntimeError, match="forbidden"):
                configure_github_repo("test/repo", "python_basic", False)

    def test_topics_raises_runtime_error_on_5xx(self):
        exc = self._make_http_error(500, "internal server error")
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=exc,
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            with pytest.raises(RuntimeError, match="internal server error"):
                configure_github_repo("test/repo", "python_basic", False)

    def test_patch_raises_runtime_error_on_4xx(self):
        # First call (topics) succeeds, second call (PATCH) fails
        resp = self._make_response(200)
        exc = self._make_http_error(404, "not found")
        side_effect = [resp, exc]
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=side_effect,
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            with pytest.raises(RuntimeError, match="not found"):
                configure_github_repo("test/repo", "python_basic", False)

    def test_get_token_none_raises_runtime_error(self):
        with patch("app.core.post_setup.get_token", return_value=None):
            with pytest.raises(RuntimeError, match="No GitHub token configured"):
                configure_github_repo("test/repo", "python_basic", False)

    def test_unknown_preset_uses_python_fallback(self):
        resp = self._make_response(200)
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            configure_github_repo("test/repo", "unknown_preset", False)

        topics_req = mock_urlopen.call_args_list[0][0][0]
        body = json.loads(topics_req.data)
        assert body == {"names": ["python"]}  # fallback


class TestDeleteGithubRepo:
    def _make_response(self, status: int = 200) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        cm.read = MagicMock(return_value=b"")
        type(cm).status = status
        return cm

    def _make_http_error(self, code: int, body: str) -> urllib.error.HTTPError:
        fp = MagicMock()
        fp.read = MagicMock(return_value=body.encode())
        return urllib.error.HTTPError(
            "https://api.github.com/repos/test/repo",
            code,
            "error",
            {},
            fp,
        )

    def test_deletes_repo_on_success(self, capsys):
        resp = self._make_response(200)
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            delete_github_repo("testuser/myrepo")

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.method == "DELETE"
        assert req.full_url == "https://api.github.com/repos/testuser/myrepo"
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_warns_on_http_error(self, capsys):
        exc = self._make_http_error(404, "Not Found")
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=exc,
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            delete_github_repo("testuser/myrepo")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "404" in captured.err
        # Non-JSON body falls back to str(exc) — "HTTP Error 404: Not Found"
        assert "404" in captured.err

    def test_warns_on_oserror(self, capsys):
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=OSError("network unreachable"),
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            delete_github_repo("testuser/myrepo")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "network unreachable" in captured.err

    def test_warns_when_no_token(self, capsys):
        with patch("app.core.post_setup.get_token", return_value=None):
            delete_github_repo("testuser/myrepo")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "no GitHub token" in captured.err

    def test_warns_on_invalid_format(self, capsys):
        delete_github_repo("badformat")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "invalid format" in captured.err

    def test_warns_on_invalid_format_multiple_slashes(self, capsys):
        delete_github_repo("test/user/myrepo")

        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_noop_when_no_repo_name(self, capsys):
        delete_github_repo("")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
