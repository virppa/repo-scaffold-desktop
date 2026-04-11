import pytest

from app.core.config import RepoConfig
from app.core.generator import generate


@pytest.fixture()
def output_dir(tmp_path):
    return tmp_path / "output"


@pytest.fixture()
def basic_config():
    return RepoConfig(repo_name="my-project", preset="python_basic")


def test_required_files_are_written(basic_config, output_dir):
    generate(basic_config, output_dir)
    assert (output_dir / "README.md").exists()
    assert (output_dir / "pyproject.toml").exists()
    assert (output_dir / ".gitignore").exists()
    assert (output_dir / "app" / "__init__.py").exists()
    assert (output_dir / "tests" / "__init__.py").exists()


def test_readme_contains_repo_name(basic_config, output_dir):
    generate(basic_config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "my-project" in content


def test_pyproject_contains_repo_name(basic_config, output_dir):
    generate(basic_config, output_dir)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "my-project" in content


def test_optional_files_absent_by_default(basic_config, output_dir):
    generate(basic_config, output_dir)
    assert not (output_dir / ".pre-commit-config.yaml").exists()
    assert not (output_dir / ".github" / "workflows" / "ci.yml").exists()
    assert not (output_dir / ".github" / "pull_request_template.md").exists()
    assert not (output_dir / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").exists()
    assert not (output_dir / ".github" / "CODEOWNERS").exists()
    assert not (output_dir / "CLAUDE.md").exists()
    assert not (output_dir / ".mcp.json").exists()


def test_precommit_file_written_when_toggled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_precommit=True
    )
    generate(config, output_dir)
    assert (output_dir / ".pre-commit-config.yaml").exists()


def test_ci_file_written_when_toggled(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic", include_ci=True)
    generate(config, output_dir)
    assert (output_dir / ".github" / "workflows" / "ci.yml").exists()


def test_claude_files_written_when_toggled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_claude_files=True
    )
    generate(config, output_dir)
    assert (output_dir / "CLAUDE.md").exists()
    assert (output_dir / ".mcp.json").exists()


def test_pr_template_written_when_toggled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_pr_template=True
    )
    generate(config, output_dir)
    assert (output_dir / ".github" / "pull_request_template.md").exists()


def test_issue_templates_written_when_toggled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_issue_templates=True
    )
    generate(config, output_dir)
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").exists()
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").exists()


def test_codeowners_written_when_toggled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_codeowners=True
    )
    generate(config, output_dir)
    assert (output_dir / ".github" / "CODEOWNERS").exists()


def test_python_desktop_required_files_written(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    assert (output_dir / "pyproject.toml").exists()
    assert (output_dir / "README.md").exists()
    assert (output_dir / ".gitignore").exists()
    assert (output_dir / "app" / "__init__.py").exists()
    assert (output_dir / "app" / "main.py").exists()
    assert (output_dir / "app" / "ui" / "__init__.py").exists()
    assert (output_dir / "tests" / "__init__.py").exists()


def test_python_desktop_pyproject_contains_pyside6(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "pyside6" in content


def test_python_desktop_ci_toggle(output_dir):
    config = RepoConfig(
        repo_name="my-desktop-app", preset="python_desktop", include_ci=True
    )
    generate(config, output_dir)
    assert (output_dir / ".github" / "workflows" / "lint-and-test.yml").exists()


def test_full_agentic_required_files_written(output_dir):
    config = RepoConfig(repo_name="my-agentic-project", preset="full_agentic")
    generate(config, output_dir)
    assert (output_dir / "pyproject.toml").exists()
    assert (output_dir / "README.md").exists()
    assert (output_dir / ".gitignore").exists()
    assert (output_dir / "app" / "__init__.py").exists()
    assert (output_dir / "tests" / "__init__.py").exists()
    assert (output_dir / "CLAUDE.md").exists()
    assert (output_dir / ".mcp.json").exists()
    assert (output_dir / ".claude" / "settings.json").exists()


def test_full_agentic_ci_toggle(output_dir):
    config = RepoConfig(
        repo_name="my-agentic-project", preset="full_agentic", include_ci=True
    )
    generate(config, output_dir)
    assert (output_dir / ".github" / "workflows" / "lint-and-test.yml").exists()


def test_all_toggles_enabled(output_dir):
    config = RepoConfig(
        repo_name="my-project",
        preset="python_basic",
        include_precommit=True,
        include_ci=True,
        include_pr_template=True,
        include_issue_templates=True,
        include_codeowners=True,
        include_claude_files=True,
    )
    generate(config, output_dir)
    assert (output_dir / "README.md").exists()
    assert (output_dir / ".pre-commit-config.yaml").exists()
    assert (output_dir / ".github" / "workflows" / "ci.yml").exists()
    assert (output_dir / ".github" / "pull_request_template.md").exists()
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").exists()
    assert (output_dir / ".github" / "CODEOWNERS").exists()
    assert (output_dir / "CLAUDE.md").exists()
    assert (output_dir / ".mcp.json").exists()
