from dataclasses import dataclass, field

_F_PYPROJECT = "pyproject.toml"
_F_README = "README.md"
_F_GITIGNORE = ".gitignore"
_F_APP_INIT = "app/__init__.py"
_F_TESTS_INIT = "tests/__init__.py"
_F_PRECOMMIT = ".pre-commit-config.yaml"
_F_CI = ".github/workflows/lint-and-test.yml"
_F_PR_TEMPLATE = ".github/pull_request_template.md"
_F_BUG_REPORT = ".github/ISSUE_TEMPLATE/bug_report.md"
_F_FEATURE_REQUEST = ".github/ISSUE_TEMPLATE/feature_request.md"
_F_CODEOWNERS = ".github/CODEOWNERS"
_F_CLAUDE_MD = "CLAUDE.md"
_F_MCP_JSON = ".mcp.json"


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    required_files: tuple[str, ...]
    # Keys must match RepoConfig boolean toggle field names
    optional_files: dict[str, tuple[str, ...]] = field(default_factory=dict)
    skills_source: str | None = None
    skills_version: str | None = None
    # Template context overrides applied by the CLI when no explicit flag is given
    context_defaults: dict[str, bool] = field(default_factory=dict)


_PRESETS: dict[str, Preset] = {
    "python_basic": Preset(
        name="python_basic",
        description="Minimal Python project with ruff, pytest, and bandit.",
        required_files=(
            _F_PYPROJECT,
            _F_README,
            _F_GITIGNORE,
            _F_APP_INIT,
            _F_TESTS_INIT,
        ),
        optional_files={
            "include_precommit": (_F_PRECOMMIT,),
            "include_ci": (_F_CI,),
            "include_pr_template": (_F_PR_TEMPLATE,),
            "include_issue_templates": (
                _F_BUG_REPORT,
                _F_FEATURE_REQUEST,
            ),
            "include_codeowners": (_F_CODEOWNERS,),
            "include_claude_files": (
                _F_CLAUDE_MD,
                _F_MCP_JSON,
            ),
        },
    ),
    "python_desktop": Preset(
        name="python_desktop",
        description="Python desktop app with PySide6, ruff, pytest, and bandit.",
        required_files=(
            _F_PYPROJECT,
            _F_README,
            _F_GITIGNORE,
            _F_APP_INIT,
            "app/main.py",
            "app/ui/__init__.py",
            _F_TESTS_INIT,
        ),
        optional_files={
            "include_precommit": (_F_PRECOMMIT,),
            "include_ci": (_F_CI,),
            "include_pr_template": (_F_PR_TEMPLATE,),
            "include_issue_templates": (
                _F_BUG_REPORT,
                _F_FEATURE_REQUEST,
            ),
            "include_codeowners": (_F_CODEOWNERS,),
            "include_claude_files": (
                _F_CLAUDE_MD,
                _F_MCP_JSON,
            ),
        },
    ),
    "full_agentic": Preset(
        name="full_agentic",
        description=(
            "Python project with Claude Code agentic workflow"
            " (Linear MCP, hooks, slash commands)."
        ),
        skills_source="github:virppa/repo-scaffold-skills",
        skills_version="v1.0.0",
        context_defaults={"include_linear_mcp": True},
        required_files=(
            _F_PYPROJECT,
            _F_README,
            _F_GITIGNORE,
            _F_APP_INIT,
            _F_TESTS_INIT,
            _F_CLAUDE_MD,
            _F_MCP_JSON,
            ".claude/settings.json",
        ),
        optional_files={
            "include_precommit": (_F_PRECOMMIT,),
            "include_ci": (_F_CI,),
            "include_pr_template": (_F_PR_TEMPLATE,),
            "include_issue_templates": (
                _F_BUG_REPORT,
                _F_FEATURE_REQUEST,
            ),
            "include_codeowners": (_F_CODEOWNERS,),
            "include_claude_files": (
                _F_CLAUDE_MD,
                _F_MCP_JSON,
                ".claude/settings.json",
            ),
        },
    ),
}


def get_preset(name: str) -> Preset:
    try:
        return _PRESETS[name]
    except KeyError:
        available = ", ".join(sorted(_PRESETS))
        raise ValueError(
            f"Unknown preset {name!r}. Available presets: {available}"
        ) from None
