import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import RepoConfig
from app.core.generator import generate
from app.core.post_setup import fetch_skills
from app.core.user_prefs import UserPreferences


@pytest.fixture()
def output_dir(tmp_path):
    return tmp_path / "output"


@pytest.fixture()
def basic_config():
    return RepoConfig(repo_name="my-project", preset="python_basic")


@pytest.fixture()
def agentic_prefs():
    return UserPreferences(author_name="Test Author")


def _make_commands_urlopen_mock(
    commands: list[str], file_content: bytes = b"# command"
):
    call_count = 0
    entries = [{"path": p, "type": "blob"} for p in commands]

    def fake_urlopen(req_or_url, timeout=None):  # noqa: ARG001
        nonlocal call_count
        cm = MagicMock()
        cm.__enter__ = lambda s: s
        cm.__exit__ = MagicMock(return_value=False)
        if call_count == 0:
            cm.read = MagicMock(return_value=json.dumps({"tree": entries}).encode())
        else:
            cm.read = MagicMock(return_value=file_content)
        call_count += 1
        return cm

    return fake_urlopen


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


def test_mcp_json_has_linear_mcp_when_enabled(output_dir):
    config = RepoConfig(
        repo_name="my-agentic-project", preset="full_agentic", include_linear_mcp=True
    )
    generate(config, output_dir)
    raw = (output_dir / ".mcp.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "_comment" in data
    assert "linear" in data["mcpServers"]
    assert data["mcpServers"]["linear"]["url"] == "https://mcp.linear.app/mcp"


def test_mcp_json_has_empty_mcp_servers_when_disabled(output_dir):
    config = RepoConfig(
        repo_name="my-project", preset="python_basic", include_claude_files=True
    )
    generate(config, output_dir)
    raw = (output_dir / ".mcp.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["mcpServers"] == {}


def test_full_agentic_settings_json_hook_structure(output_dir, agentic_prefs):
    config = RepoConfig(
        repo_name="my-agentic-project", preset="full_agentic", include_linear_mcp=True
    )
    generate(config, output_dir, prefs=agentic_prefs)
    raw = (output_dir / ".claude" / "settings.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    hooks = data["hooks"]
    assert "PostToolUse" in hooks, "hooks missing PostToolUse key"
    assert "Stop" in hooks, "hooks missing Stop key"
    assert "PreToolUse" in hooks, "hooks missing PreToolUse key"


def test_full_agentic_commands_files_present(output_dir, agentic_prefs):
    config = RepoConfig(
        repo_name="my-agentic-project", preset="full_agentic", include_linear_mcp=True
    )
    generate(config, output_dir, prefs=agentic_prefs)
    expected = [
        ".claude/commands/groom-ticket.md",
        ".claude/commands/start-ticket.md",
        ".claude/commands/finalize-ticket.md",
        ".claude/commands/security-check.md",
    ]
    with patch(
        "app.core.post_setup.urllib.request.urlopen",
        side_effect=_make_commands_urlopen_mock(expected),
    ):
        fetch_skills(
            output_dir,
            skills_source="github:virppa/repo-scaffold-skills",
            skills_version="v1.0.0",
        )
    for path in expected:
        assert (output_dir / path).exists(), f"Missing command file: {path}"


def test_full_agentic_claude_md_section_headings(output_dir, agentic_prefs):
    config = RepoConfig(
        repo_name="my-agentic-project", preset="full_agentic", include_linear_mcp=True
    )
    generate(config, output_dir, prefs=agentic_prefs)
    content = (output_dir / "CLAUDE.md").read_text(encoding="utf-8")
    for heading in ("## Commands", "## Architecture", "## Development workflow"):
        assert heading in content, f"CLAUDE.md missing section heading: {heading}"


def test_full_agentic_no_unrendered_jinja_variables(output_dir, agentic_prefs):
    config = RepoConfig(
        repo_name="my-agentic-project", preset="full_agentic", include_linear_mcp=True
    )
    written = generate(config, output_dir, prefs=agentic_prefs)
    for rel_path in written:
        content = (output_dir / rel_path).read_text(encoding="utf-8", errors="replace")
        assert "{{" not in content, f"Unrendered Jinja2 variable in {rel_path}"


def test_python_basic_dev_deps_include_mock_and_snapshot(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    generate(config, output_dir)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "pytest-mock" in content
    assert "pytest-snapshot" in content


def test_python_basic_dev_deps_exclude_qt_and_asyncio(output_dir):
    config = RepoConfig(repo_name="my-project", preset="python_basic")
    generate(config, output_dir)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "pytest-qt" not in content
    assert "pytest-asyncio" not in content


def test_python_desktop_dev_deps_include_qt(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert "pytest-mock" in content
    assert "pytest-snapshot" in content
    assert "pytest-qt" in content
    assert "pytest-asyncio" not in content


def test_full_agentic_dev_deps_include_all_plugins(output_dir):
    config = RepoConfig(repo_name="my-agentic-project", preset="full_agentic")
    generate(config, output_dir)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    for plugin in (
        "pytest-mock",
        "pytest-snapshot",
        "pytest-qt",
        "pytest-asyncio",
        "pytest-httpx",
        "hypothesis",
    ):
        assert plugin in content, f"pyproject.toml missing test plugin: {plugin}"


def test_full_agentic_asyncio_mode_auto(output_dir):
    config = RepoConfig(repo_name="my-agentic-project", preset="full_agentic")
    generate(config, output_dir)
    content = (output_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert 'asyncio_mode = "auto"' in content


def test_python_desktop_generates_conftest(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    assert (output_dir / "tests" / "conftest.py").exists()


def test_python_desktop_conftest_has_qapp_fixture(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    content = (output_dir / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "qapp" in content
    assert "QApplication" in content


def test_python_desktop_generates_ui_smoke_test(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    assert (output_dir / "tests" / "test_ui_smoke.py").exists()


def test_python_desktop_ui_smoke_contains_repo_name(output_dir):
    config = RepoConfig(repo_name="my-desktop-app", preset="python_desktop")
    generate(config, output_dir)
    content = (output_dir / "tests" / "test_ui_smoke.py").read_text(encoding="utf-8")
    assert "my-desktop-app" in content
