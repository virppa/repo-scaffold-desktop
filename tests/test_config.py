import pytest
from pydantic import ValidationError

from app.core.config import RepoConfig


def test_valid_config_minimal():
    config = RepoConfig(repo_name="my-repo", preset="python_basic")
    assert config.repo_name == "my-repo"
    assert config.preset == "python_basic"


def test_valid_config_all_options():
    config = RepoConfig(
        repo_name="my-repo",
        preset="python_basic",
        include_precommit=True,
        include_ci=True,
        include_pr_template=True,
        include_issue_templates=True,
        include_codeowners=True,
        include_claude_files=True,
    )
    assert config.include_precommit is True
    assert config.include_ci is True
    assert config.include_pr_template is True
    assert config.include_issue_templates is True
    assert config.include_codeowners is True
    assert config.include_claude_files is True


def test_option_defaults():
    config = RepoConfig(repo_name="my-repo", preset="python_basic")
    assert config.include_precommit is False
    assert config.include_ci is False
    assert config.include_pr_template is False
    assert config.include_issue_templates is False
    assert config.include_codeowners is False
    assert config.include_claude_files is False


def test_empty_repo_name_rejected():
    with pytest.raises(ValidationError, match="repo_name"):
        RepoConfig(repo_name="", preset="python_basic")


def test_whitespace_repo_name_rejected():
    with pytest.raises(ValidationError, match="repo_name"):
        RepoConfig(repo_name="   ", preset="python_basic")


def test_repo_name_stripped_of_surrounding_whitespace():
    config = RepoConfig(repo_name="  my-repo  ", preset="python_basic")
    assert config.repo_name == "my-repo"


def test_repo_name_with_forward_slash_rejected():
    with pytest.raises(ValidationError, match="path separators"):
        RepoConfig(repo_name="foo/bar", preset="python_basic")


def test_repo_name_with_backslash_rejected():
    with pytest.raises(ValidationError, match="path separators"):
        RepoConfig(repo_name="foo\\bar", preset="python_basic")


def test_all_valid_presets_accepted():
    for preset in ("python_basic", "python_desktop", "full_agentic"):
        config = RepoConfig(repo_name="my-repo", preset=preset)
        assert config.preset == preset


def test_invalid_preset_rejected():
    with pytest.raises(ValidationError):
        RepoConfig(repo_name="my-repo", preset="nonexistent_preset")
