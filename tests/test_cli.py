from pathlib import Path
from unittest.mock import patch

import pytest

from app.cli import main
from app.core.user_prefs import PrefsStore


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
    assert "Error" in capsys.readouterr().err


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
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "generate",
                "--preset",
                "python_basic",
                "--output",
                str(output_dir),
            ]
        )
    assert exc_info.value.code != 0


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


def test_watcher_verbose_flag_forwarded(tmp_path):
    from unittest.mock import MagicMock, patch

    mock_instance = MagicMock()
    mock_instance.run.return_value = None
    # Watcher is a lazy import inside _run_watcher, so patch at source module
    with patch("app.core.watcher.Watcher", return_value=mock_instance) as MockWatcher:
        rc = main(["watcher", "--verbose"])
    assert rc == 0
    _, kwargs = MockWatcher.call_args
    assert kwargs.get("verbose") is True


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
