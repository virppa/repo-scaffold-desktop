import pytest

from app.core.config import RepoConfig
from app.core.generator import generate
from app.core.user_prefs import UserPreferences


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
    assert not (output_dir / ".github" / "workflows" / "lint-and-test.yml").exists()
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
    assert (output_dir / ".github" / "workflows" / "lint-and-test.yml").exists()


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


def test_full_agentic_settings_json_contains_hooks(output_dir):
    config = RepoConfig(repo_name="my-agentic-project", preset="full_agentic")
    generate(config, output_dir)
    content = (output_dir / ".claude" / "settings.json").read_text()
    for marker in (
        "PostToolUse",
        "PreToolUse",
        "Stop",
        "ruff",
        "bandit",
        "mypy",
        "lint-imports",
    ):
        assert marker in content, (
            f"settings.json missing expected hook marker: {marker}"
        )


def test_full_agentic_command_files_written(output_dir):
    config = RepoConfig(repo_name="my-agentic-project", preset="full_agentic")
    generate(config, output_dir)
    for cmd in (
        "groom-ticket.md",
        "start-ticket.md",
        "finalize-ticket.md",
        "security-check.md",
    ):
        assert (output_dir / ".claude" / "commands" / cmd).exists(), f"missing {cmd}"


def test_full_agentic_commands_substitute_vars(output_dir):
    config = RepoConfig(
        repo_name="my-agentic-project",
        preset="full_agentic",
        linear_project="my-linear-project",
    )
    generate(config, output_dir)
    for cmd in ("groom-ticket.md", "start-ticket.md", "finalize-ticket.md"):
        content = (output_dir / ".claude" / "commands" / cmd).read_text()
        assert "my-linear-project" in content, f"{cmd} missing linear_project value"
        assert "{{" not in content, f"{cmd} has unrendered Jinja2 variable"


def test_full_agentic_commands_fallback_to_repo_name(output_dir):
    config = RepoConfig(repo_name="my-repo", preset="full_agentic")
    generate(config, output_dir)
    content = (output_dir / ".claude" / "commands" / "groom-ticket.md").read_text()
    assert "my-repo" in content
    assert "{{" not in content


def test_other_presets_have_no_command_files(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    generate(config, output_dir)
    assert not (output_dir / ".claude" / "commands").exists()


def test_full_agentic_pyproject_contains_quality_tools(output_dir):
    config = RepoConfig(repo_name="my-agentic-project", preset="full_agentic")
    generate(config, output_dir)
    content = (output_dir / "pyproject.toml").read_text()
    for tool in ("bandit", "mypy", "lint-imports"):
        assert tool in content, f"pyproject.toml dev deps missing: {tool}"


def test_full_agentic_ci_toggle(output_dir):
    config = RepoConfig(
        repo_name="my-agentic-project", preset="full_agentic", include_ci=True
    )
    generate(config, output_dir)
    assert (output_dir / ".github" / "workflows" / "lint-and-test.yml").exists()


def test_readme_mentions_precommit_when_toggled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_precommit=True
    )
    generate(config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "pre-commit" in content


def test_readme_no_precommit_section_when_not_toggled(basic_config, output_dir):
    generate(basic_config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "pre-commit" not in content


def test_readme_mentions_ci_when_toggled(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic", include_ci=True)
    generate(config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "workflow" in content.lower() or "CI" in content


def test_readme_no_ci_section_when_not_toggled(basic_config, output_dir):
    generate(basic_config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "workflow" not in content.lower()


def test_readme_mentions_claude_when_toggled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_claude_files=True
    )
    generate(config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "Claude" in content


def test_readme_no_claude_section_when_not_toggled(basic_config, output_dir):
    generate(basic_config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "Claude" not in content


def test_python_basic_readme_does_not_contain_app_main(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    generate(config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "python -m app.main" not in content


def test_python_desktop_readme_contains_app_main(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "python -m app.main" in content


def test_full_agentic_readme_does_not_contain_app_main(output_dir):
    config = RepoConfig(repo_name="my-agentic-project", preset="full_agentic")
    generate(config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "python -m app.main" not in content


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
    assert (output_dir / ".github" / "workflows" / "lint-and-test.yml").exists()
    assert (output_dir / ".github" / "pull_request_template.md").exists()
    assert (output_dir / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").exists()
    assert (output_dir / ".github" / "CODEOWNERS").exists()
    assert (output_dir / "CLAUDE.md").exists()
    assert (output_dir / ".mcp.json").exists()


def test_shared_template_used_as_fallback(output_dir):
    # .gitignore lives only in templates/shared/, not in any preset dir
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    generate(config, output_dir)
    assert (output_dir / ".gitignore").exists()


def test_preset_template_overrides_shared(output_dir):
    # README.md.j2 exists in the preset dir; shared has no README — verify
    # the preset version is used (contains the repo name rendered by the template)
    config = RepoConfig(repo_name="override-check", preset="python_basic")
    generate(config, output_dir)
    content = (output_dir / "README.md").read_text(encoding="utf-8")
    assert "override-check" in content


def test_generate_with_prefs_injects_author_name(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    prefs = UserPreferences(author_name="Jane Doe", author_email="jane@example.com")
    generate(config, output_dir, prefs=prefs)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "Jane Doe" in content
    assert "jane@example.com" in content


def test_generate_with_prefs_injects_github_username(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_codeowners=True
    )
    prefs = UserPreferences(github_username="jdoe")
    generate(config, output_dir, prefs=prefs)
    content = (output_dir / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "@jdoe" in content


def test_generate_with_no_prefs_uses_empty_defaults(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    generate(config, output_dir)
    assert (output_dir / "pyproject.toml").exists()
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "authors" not in content


def test_generate_no_authors_section_when_prefs_empty(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    prefs = UserPreferences()
    generate(config, output_dir, prefs=prefs)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "authors" not in content
