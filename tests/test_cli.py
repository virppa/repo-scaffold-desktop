from unittest.mock import patch

import pytest

from app.cli import main


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
    assert (output_dir / ".github" / "workflows" / "ci.yml").exists()
    assert (output_dir / ".github" / "pull_request_template.md").exists()
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").exists()
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").exists()
    assert (output_dir / ".github" / "CODEOWNERS").exists()
    assert (output_dir / "CLAUDE.md").exists()
    assert (output_dir / ".mcp.json").exists()


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
