from typing import Literal

from pydantic import BaseModel, field_validator

VALID_PRESETS = ("python_basic", "python_desktop", "full_agentic")


class RepoConfig(BaseModel):
    repo_name: str
    preset: Literal["python_basic", "python_desktop", "full_agentic"]

    include_precommit: bool = False
    include_ci: bool = False
    include_pr_template: bool = False
    include_issue_templates: bool = False
    include_codeowners: bool = False
    include_claude_files: bool = False

    git_init: bool = False
    install_precommit: bool = False
    linear_project: str = ""

    @field_validator("repo_name")
    @classmethod
    def repo_name_must_be_valid(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("repo_name must not be empty or whitespace")
        if any(c in stripped for c in ("/", "\\", "\0")):
            raise ValueError("repo_name must not contain path separators")
        return stripped
