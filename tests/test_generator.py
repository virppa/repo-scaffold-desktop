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


def test_all_toggles_enabled(output_dir):
    config = RepoConfig(
        repo_name="my-project",
        preset="python_basic",
        include_precommit=True,
        include_ci=True,
        include_claude_files=True,
    )
    generate(config, output_dir)
    assert (output_dir / "README.md").exists()
    assert (output_dir / ".pre-commit-config.yaml").exists()
    assert (output_dir / ".github" / "workflows" / "ci.yml").exists()
    assert (output_dir / "CLAUDE.md").exists()
    assert (output_dir / ".mcp.json").exists()
