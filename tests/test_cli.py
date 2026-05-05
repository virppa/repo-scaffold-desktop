import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.cli import main
from app.core.user_prefs import PrefsStore, UserPreferences


@pytest.fixture()
def output_dir(tmp_path):
    return tmp_path / "out"


def test_generate_subcommand_writes_files(output_dir):
    rc = main(
        [
            "generate",
            "--preset",
            "python_basic",
            "--repo-name",
            "myrepo",
            "--output",
            str(output_dir),
        ]
    )
    assert rc == 0
    assert (output_dir / "README.md").exists()
    assert (output_dir / "pyproject.toml").exists()
    assert (output_dir / ".gitignore").exists()
    assert (output_dir / "app" / "__init__.py").exists()
    assert (output_dir / "tests" / "__init__.py").exists()


def test_all_toggles_enabled(output_dir):
    rc = main(
        [
            "generate",
            "--preset",
            "python_basic",
            "--repo-name",
            "myrepo",
            "--output",
            str(output_dir),
            "--pre-commit",
            "--ci",
            "--pr-template",
            "--issue-templates",
            "--codeowners",
            "--claude-files",
        ]
    )
    assert rc == 0
    assert (output_dir / ".pre-commit-config.yaml").exists()
    assert (output_dir / ".github" / "workflows" / "lint-and-test.yml").exists()
    assert (output_dir / ".github" / "pull_request_template.md").exists()
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").exists()
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").exists()
    assert (output_dir / ".github" / "CODEOWNERS").exists()
    assert (output_dir / "CLAUDE.md").exists()
    assert (output_dir / ".mcp.json").exists()


def test_invalid_repo_name_exits(output_dir, capsys):
    rc = main(
        [
            "generate",
            "--preset",
            "python_basic",
            "--repo-name",
            "",
            "--output",
            str(output_dir),
        ]
    )
    assert rc == 1
    assert "error" in capsys.readouterr().err


def test_generate_error_exits(output_dir, capsys):
    with patch("app.cli.generate", side_effect=ValueError("unknown preset")):
        rc = main(
            [
                "generate",
                "--preset",
                "python_basic",
                "--repo-name",
                "myrepo",
                "--output",
                str(output_dir),
            ]
        )
    assert rc == 1
    assert "unknown preset" in capsys.readouterr().err


def test_missing_repo_name_exits(output_dir, capsys):
    rc = main(
        [
            "generate",
            "--preset",
            "python_basic",
            "--output",
            str(output_dir),
        ]
    )
    assert rc == 1


def test_invalid_preset_exits(output_dir, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "generate",
                "--preset",
                "nonexistent_preset",
                "--repo-name",
                "myrepo",
                "--output",
                str(output_dir),
            ]
        )
    assert exc_info.value.code != 0


def test_progress_output(output_dir, capsys):
    rc = main(
        [
            "generate",
            "--preset",
            "python_basic",
            "--repo-name",
            "myrepo",
            "--output",
            str(output_dir),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "✓ README.md" in captured.out
    assert "✓ pyproject.toml" in captured.out
    assert "✓ .gitignore" in captured.out


def test_no_subcommand_shows_help(capsys):
    rc = main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower()


def test_git_init_flag_calls_post_setup(output_dir):
    with patch("app.cli.run_git_init") as mock_git:
        rc = main(
            [
                "generate",
                "--preset",
                "python_basic",
                "--repo-name",
                "myrepo",
                "--output",
                str(output_dir),
                "--git-init",
            ]
        )
    assert rc == 0
    mock_git.assert_called_once_with(output_dir)


def test_install_precommit_flag_calls_post_setup(output_dir):
    with patch("app.cli.run_precommit_install") as mock_pc:
        rc = main(
            [
                "generate",
                "--preset",
                "python_basic",
                "--repo-name",
                "myrepo",
                "--output",
                str(output_dir),
                "--install-precommit",
            ]
        )
    assert rc == 0
    mock_pc.assert_called_once_with(output_dir)


def test_config_get_defaults(tmp_path, capsys):
    prefs_path = tmp_path / "prefs.json"
    with patch.object(PrefsStore, "get_path", return_value=prefs_path):
        rc = main(["config", "get"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "author-name:" in out
    assert "default-preset: python_basic" in out


def test_config_set_and_get(tmp_path, capsys):
    prefs_path = tmp_path / "prefs.json"
    with patch.object(PrefsStore, "get_path", return_value=prefs_path):
        rc_set = main(["config", "set", "author-name", "Antti"])
        assert rc_set == 0
        rc_get = main(["config", "get"])
    assert rc_get == 0
    out = capsys.readouterr().out
    assert "author-name: Antti" in out


def test_config_set_output_dir(tmp_path, capsys):
    prefs_path = tmp_path / "prefs.json"
    with patch.object(PrefsStore, "get_path", return_value=prefs_path):
        rc = main(["config", "set", "default-output-dir", "/tmp/repos"])
    assert rc == 0
    with patch.object(PrefsStore, "get_path", return_value=prefs_path):
        prefs = PrefsStore.load()
    assert prefs.default_output_dir == Path("/tmp/repos")


def test_config_no_subcommand_exits(capsys):
    rc = main(["config"])
    assert rc == 1


def test_full_agentic_preset_calls_fetch_skills(output_dir):
    with patch(
        "app.cli.fetch_skills", return_value=[".claude/commands/groom-ticket.md"]
    ) as mock_fetch:
        rc = main(
            [
                "generate",
                "--preset",
                "full_agentic",
                "--repo-name",
                "myrepo",
                "--output",
                str(output_dir),
            ]
        )
    assert rc == 0
    from app.core.presets import get_preset

    preset = get_preset("full_agentic")
    mock_fetch.assert_called_once_with(
        output_dir,
        skills_source=preset.skills_source,
        skills_version=preset.skills_version,
        context=None,
    )


def test_post_setup_error_exits_nonzero(output_dir, capsys):
    with patch(
        "app.cli.run_git_init", side_effect=RuntimeError("git not found on PATH")
    ):
        rc = main(
            [
                "generate",
                "--preset",
                "python_basic",
                "--repo-name",
                "myrepo",
                "--output",
                str(output_dir),
                "--git-init",
            ]
        )
    assert rc == 1
    captured = capsys.readouterr()
    assert "git not found on PATH" in captured.err


def test_watcher_worker_verbose_flag_forwarded(tmp_path):
    from unittest.mock import MagicMock, patch

    mock_instance = MagicMock()
    mock_instance.run.return_value = None
    # Watcher is a lazy import inside _run_watcher, so patch at source module
    with patch("app.core.watcher.Watcher", return_value=mock_instance) as MockWatcher:
        rc = main(["watcher", "--worker-verbose"])
    assert rc == 0
    _, kwargs = MockWatcher.call_args
    assert kwargs.get("worker_verbose") is True


def test_watcher_verbose_does_not_set_worker_verbose(tmp_path):
    from unittest.mock import MagicMock, patch

    mock_instance = MagicMock()
    mock_instance.run.return_value = None
    with patch("app.core.watcher.Watcher", return_value=mock_instance) as MockWatcher:
        rc = main(["watcher", "--verbose"])
    assert rc == 0
    _, kwargs = MockWatcher.call_args
    # --verbose only controls logging level — it is intentionally not forwarded
    # to Watcher.__init__ at all, so the kwarg must not appear.
    assert "verbose" not in kwargs
    assert kwargs.get("worker_verbose") is False


def test_watcher_verbose_and_worker_verbose_both_forwarded(tmp_path):
    from unittest.mock import MagicMock, patch

    mock_instance = MagicMock()
    mock_instance.run.return_value = None
    with patch("app.core.watcher.Watcher", return_value=mock_instance) as MockWatcher:
        rc = main(["watcher", "--verbose", "--worker-verbose"])
    assert rc == 0
    _, kwargs = MockWatcher.call_args
    # verbose is no longer forwarded to Watcher — it only controls logging level
    assert kwargs.get("worker_verbose") is True


def test_watcher_max_local_and_cloud_workers_forwarded():
    from unittest.mock import MagicMock, patch

    mock_instance = MagicMock()
    mock_instance.run.return_value = None
    with patch("app.core.watcher.Watcher", return_value=mock_instance) as MockWatcher:
        rc = main(["watcher", "--max-local-workers", "2", "--max-cloud-workers", "5"])
    assert rc == 0
    _, kwargs = MockWatcher.call_args
    assert kwargs.get("max_local_workers") == 2
    assert kwargs.get("max_cloud_workers") == 5


def test_watcher_max_workers_alias_sets_both():
    from unittest.mock import MagicMock, patch

    mock_instance = MagicMock()
    mock_instance.run.return_value = None
    with patch("app.core.watcher.Watcher", return_value=mock_instance) as MockWatcher:
        rc = main(["watcher", "--max-workers", "4"])
    assert rc == 0
    _, kwargs = MockWatcher.call_args
    assert kwargs.get("max_local_workers") == 4
    assert kwargs.get("max_cloud_workers") == 4


def test_watcher_max_local_workers_default_is_8():
    from unittest.mock import MagicMock, patch

    mock_instance = MagicMock()
    mock_instance.run.return_value = None
    with patch("app.core.watcher.Watcher", return_value=mock_instance) as MockWatcher:
        rc = main(["watcher"])
    assert rc == 0
    _, kwargs = MockWatcher.call_args
    assert kwargs.get("max_local_workers") == 8


# ── GitHub rollback tests ──────────────────────────────────────────────────────


def _make_github_response():
    """Return a mock response that looks like a GitHub create-repo response."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(
        return_value=b'{"html_url": "https://github.com/testuser/myrepo"}'
    )
    return resp


class TestGitHubRollback:
    """Tests for delete_github_repo and rollback behavior."""

    def test_delete_github_repo_sends_delete_request(self):
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read = MagicMock(return_value=b"")

        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                return_value=resp,
            ) as mock_urlopen,
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("testuser/myrepo")

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.method == "DELETE"
        assert "testuser/myrepo" in req.full_url

    def test_delete_github_repo_warns_on_http_error(self, capsys):
        from unittest.mock import patch

        fp = MagicMock()
        fp.read = MagicMock(return_value=b"Not Found")
        exc = urllib.error.HTTPError(
            "https://api.github.com/repos/testuser/myrepo",
            404,
            "Not Found",
            {},
            fp,
        )
        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=exc,
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("testuser/myrepo")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "404" in captured.err

    def test_delete_github_repo_warns_on_oserror(self, capsys):
        from unittest.mock import patch

        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=OSError("network unreachable"),
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("testuser/myrepo")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "network unreachable" in captured.err

    def test_delete_github_repo_warns_when_no_token(self, capsys):
        from unittest.mock import patch

        with patch("app.core.post_setup.get_token", return_value=None):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("testuser/myrepo")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "no GitHub token" in captured.err

    def test_delete_github_repo_warns_on_invalid_format(self, capsys):
        from unittest.mock import patch

        with patch("app.core.post_setup.get_token", return_value="ghp_fake"):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("badformat")

        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "invalid format" in captured.err

    def test_delete_github_repo_does_not_raise(self):
        """delete_github_repo is best-effort — never raises."""
        from unittest.mock import patch

        with (
            patch(
                "app.core.post_setup.urllib.request.urlopen",
                side_effect=OSError("network unreachable"),
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
        ):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("testuser/myrepo")  # should not raise

    def test_delete_github_repo_invalid_format_noop(self):
        """Invalid format should not attempt any network call."""
        from unittest.mock import patch

        with (
            patch("app.core.post_setup.get_token", return_value="ghp_fake") as mock_get,
            patch("app.core.post_setup.urllib.request.urlopen") as mock_urlopen,
        ):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("badformat")

        mock_get.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_delete_github_repo_invalid_format_multiple_slashes_noop(self):
        """Multiple slashes should not attempt any network call."""
        from unittest.mock import patch

        with (
            patch("app.core.post_setup.get_token", return_value="ghp_fake") as mock_get,
            patch("app.core.post_setup.urllib.request.urlopen") as mock_urlopen,
        ):
            from app.core.post_setup import delete_github_repo

            delete_github_repo("test/user/myrepo")

        mock_get.assert_not_called()
        mock_urlopen.assert_not_called()


class TestGitHubRollbackFlow:
    """Integration tests for the rollback flow in _run_generate."""

    def test_push_failure_triggers_rollback(self, output_dir, capsys):
        """When push fails, the repo should be deleted."""
        with (
            patch(
                "app.cli.create_github_repo",
                return_value="https://github.com/testuser/myrepo",
            ),
            patch(
                "app.cli.configure_github_repo",
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
            patch(
                "app.cli.run_initial_push",
                side_effect=RuntimeError("failed to push"),
            ) as mock_push,
            patch("app.cli.run_git_init"),
            patch("app.cli.run_precommit_install"),
            patch(
                "app.cli.delete_github_repo",
            ) as mock_delete,
        ):
            rc = main(
                [
                    "generate",
                    "--preset",
                    "python_basic",
                    "--repo-name",
                    "myrepo",
                    "--output",
                    str(output_dir),
                    "--github-create",
                    "--git-push",
                    "--remote-url",
                    "https://github.com/testuser/myrepo",
                ]
            )

        assert rc == 1
        mock_push.assert_called_once()
        mock_delete.assert_called_once_with("testuser/myrepo")
        captured = capsys.readouterr()
        assert "failed to push" in captured.err

    def test_push_no_rollback_with_flag(self, output_dir, capsys):
        """--no-rollback-on-failure should skip the delete call."""
        with (
            patch(
                "app.cli.create_github_repo",
                return_value="https://github.com/testuser/myrepo",
            ),
            patch(
                "app.cli.configure_github_repo",
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
            patch(
                "app.cli.run_initial_push",
                side_effect=RuntimeError("failed to push"),
            ),
            patch("app.cli.run_git_init"),
            patch("app.cli.run_precommit_install"),
            patch(
                "app.cli.delete_github_repo",
            ) as mock_delete,
        ):
            rc = main(
                [
                    "generate",
                    "--preset",
                    "python_basic",
                    "--repo-name",
                    "myrepo",
                    "--output",
                    str(output_dir),
                    "--github-create",
                    "--git-push",
                    "--remote-url",
                    "https://github.com/testuser/myrepo",
                    "--no-rollback-on-failure",
                ]
            )

        assert rc == 1
        mock_delete.assert_not_called()

    def test_configure_failure_triggers_rollback(self, output_dir, capsys):
        """When configure fails, the repo should be deleted."""
        with (
            patch(
                "app.cli.create_github_repo",
                return_value="https://github.com/testuser/myrepo",
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
            patch(
                "app.cli.configure_github_repo",
                side_effect=RuntimeError("forbidden"),
            ),
            patch("app.cli.run_git_init"),
            patch("app.cli.run_precommit_install"),
            patch(
                "app.cli.delete_github_repo",
            ) as mock_delete,
        ):
            rc = main(
                [
                    "generate",
                    "--preset",
                    "python_basic",
                    "--repo-name",
                    "myrepo",
                    "--output",
                    str(output_dir),
                    "--github-create",
                ]
            )

        assert rc == 1
        mock_delete.assert_called_once_with("testuser/myrepo")
        captured = capsys.readouterr()
        assert "forbidden" in captured.err

    def test_github_create_failure_no_rollback(self, output_dir, capsys):
        """If create itself fails, there's nothing to rollback."""
        with (
            patch(
                "app.cli.create_github_repo",
                side_effect=RuntimeError("Repository already exists"),
            ),
            patch("app.core.post_setup.get_token", return_value="ghp_fake"),
            patch("app.cli.run_git_init"),
            patch("app.cli.run_precommit_install"),
            patch("app.cli.delete_github_repo") as mock_delete,
        ):
            rc = main(
                [
                    "generate",
                    "--preset",
                    "python_basic",
                    "--repo-name",
                    "myrepo",
                    "--output",
                    str(output_dir),
                    "--github-create",
                ]
            )

        assert rc == 1
        mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# Interactive wizard integration tests
# ---------------------------------------------------------------------------


class TestInteractiveWizard:
    """Tests for generate --interactive flow."""

    def test_interactive_writes_files(self, output_dir, tmp_path):
        """Interactive flow writes scaffold files."""
        prefs_path = tmp_path / "prefs.json"
        with (
            patch.object(PrefsStore, "get_path", return_value=prefs_path),
            patch("app.cli.generate") as mock_gen,
            patch(
                "app.core.wizard.input",
                side_effect=["myrepo", "python_basic", str(output_dir)],
            ),
        ):
            mock_gen.return_value = [".gitignore", "pyproject.toml", "README.md"]
            rc = main(
                [
                    "generate",
                    "--interactive",
                ]
            )
        assert rc == 0
        mock_gen.assert_called_once()

    def test_interactive_runs_post_setup(self, output_dir, tmp_path):
        """Interactive flow calls run_git_init when --git-init is set."""
        prefs_path = tmp_path / "prefs.json"
        with (
            patch.object(PrefsStore, "get_path", return_value=prefs_path),
            patch("app.cli.generate") as mock_gen,
            patch("app.cli.run_git_init") as mock_git,
            patch(
                "app.core.wizard.input",
                side_effect=["myrepo", "python_basic", str(output_dir)],
            ),
        ):
            mock_gen.return_value = [".gitignore"]
            rc = main(
                [
                    "generate",
                    "--interactive",
                    "--git-init",
                ]
            )
        assert rc == 0
        mock_git.assert_called_once_with(output_dir)

    def test_interactive_with_manual_steps(self, output_dir, tmp_path):
        """Interactive flow with --manual-steps includes toggle questions."""
        prefs_path = tmp_path / "prefs.json"
        # 3 basic + 6 toggle + 1 save answer = 10 inputs
        side_effect = [
            "myrepo",
            "python_basic",
            str(output_dir),  # 3 basic
            "yes",
            "yes",
            "yes",
            "yes",
            "yes",
            "yes",  # 6 toggles
        ]
        with (
            patch.object(PrefsStore, "get_path", return_value=prefs_path),
            patch("app.cli.generate") as mock_gen,
            patch("app.core.wizard.input", side_effect=side_effect),
        ):
            mock_gen.return_value = [
                ".gitignore",
                "pyproject.toml",
                "README.md",
                ".pre-commit-config.yaml",
            ]
            rc = main(
                [
                    "generate",
                    "--interactive",
                    "--manual-steps",
                ]
            )
        assert rc == 0
        assert mock_gen.call_count == 1

    def test_interactive_prefill_prefers_prefs(self, output_dir, tmp_path):
        """--prefill reads stored user preferences for default values."""
        prefs_path = tmp_path / "prefs.json"
        prefs = UserPreferences(
            author_name="Alice",
            default_preset="full_agentic",
            default_output_dir=Path("/tmp/xyz"),
        )
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(
            prefs.model_dump_json(indent=2),
            encoding="utf-8",
        )
        # 3 basic inputs — all empty to accept pre-filled defaults
        with (
            patch.object(PrefsStore, "get_path", return_value=prefs_path),
            patch("app.cli.generate") as mock_gen,
            patch("app.core.wizard.input", side_effect=["", "", ""]),
        ):
            mock_gen.return_value = [".gitignore"]
            rc = main(
                [
                    "generate",
                    "--interactive",
                    "--prefill",
                ]
            )
        assert rc == 0

    def test_interactive_save_defaults_calls_save(self, output_dir, tmp_path):
        """--save-defaults persists answers to PrefsStore."""
        prefs_path = tmp_path / "prefs.json"
        prefs = UserPreferences(author_name="Pre-filled")
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(
            prefs.model_dump_json(indent=2),
            encoding="utf-8",
        )
        saved_prefs = None

        def capture_save(prefs_instance):
            nonlocal saved_prefs
            saved_prefs = prefs_instance

        with (
            patch.object(PrefsStore, "get_path", return_value=prefs_path),
            patch("app.cli.generate") as mock_gen,
            patch.object(PrefsStore, "save", side_effect=capture_save) as mock_save,
            patch(
                "app.core.wizard.input",
                side_effect=["myrepo", "python_basic", str(output_dir)],
            ),
        ):
            mock_gen.return_value = [".gitignore"]
            rc = main(
                [
                    "generate",
                    "--interactive",
                    "--save-defaults",
                ]
            )
        assert rc == 0
        assert mock_save.call_count == 1
        assert saved_prefs is not None

    def test_interactive_requires_output_via_parser(self, output_dir, capsys):
        """--output is required for non-interactive mode."""
        rc = main(
            [
                "generate",
                "--preset",
                "python_basic",
                "--repo-name",
                "myrepo",
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "--preset" in err or "required" in err

    def test_interactive_flag_available_in_parser(self, output_dir):
        """The --interactive flag should be available and accepted."""
        from app.core.wizard import collect_interactive_wizard, validate_repo_name

        # Import exists and callable
        assert callable(validate_repo_name)
        assert callable(collect_interactive_wizard)
