from dataclasses import dataclass, field


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    required_files: tuple[str, ...]
    # Keys must match RepoConfig boolean toggle field names
    optional_files: dict[str, tuple[str, ...]] = field(default_factory=dict)


_PRESETS: dict[str, Preset] = {
    "python_basic": Preset(
        name="python_basic",
        description="Minimal Python project with ruff, pytest, and bandit.",
        required_files=(
            "pyproject.toml",
            "README.md",
            ".gitignore",
            "app/__init__.py",
            "tests/__init__.py",
        ),
        optional_files={
            "include_precommit": (".pre-commit-config.yaml",),
            "include_ci": (".github/workflows/lint-and-test.yml",),
            "include_pr_template": (".github/pull_request_template.md",),
            "include_issue_templates": (
                ".github/ISSUE_TEMPLATE/bug_report.md",
                ".github/ISSUE_TEMPLATE/feature_request.md",
            ),
            "include_codeowners": (".github/CODEOWNERS",),
            "include_claude_files": (
                "CLAUDE.md",
                ".mcp.json",
            ),
        },
    ),
    "python_desktop": Preset(
        name="python_desktop",
        description="Python desktop app with PySide6, ruff, pytest, and bandit.",
        required_files=(
            "pyproject.toml",
            "README.md",
            ".gitignore",
            "app/__init__.py",
            "app/main.py",
            "app/ui/__init__.py",
            "tests/__init__.py",
        ),
        optional_files={
            "include_precommit": (".pre-commit-config.yaml",),
            "include_ci": (".github/workflows/lint-and-test.yml",),
            "include_pr_template": (".github/pull_request_template.md",),
            "include_issue_templates": (
                ".github/ISSUE_TEMPLATE/bug_report.md",
                ".github/ISSUE_TEMPLATE/feature_request.md",
            ),
            "include_codeowners": (".github/CODEOWNERS",),
            "include_claude_files": (
                "CLAUDE.md",
                ".mcp.json",
            ),
        },
    ),
    "full_agentic": Preset(
        name="full_agentic",
        description=(
            "Python project with Claude Code agentic workflow"
            " (Linear MCP, hooks, slash commands)."
        ),
        required_files=(
            "pyproject.toml",
            "README.md",
            ".gitignore",
            "app/__init__.py",
            "tests/__init__.py",
            "CLAUDE.md",
            ".mcp.json",
            ".claude/settings.json",
        ),
        optional_files={
            "include_precommit": (".pre-commit-config.yaml",),
            "include_ci": (".github/workflows/lint-and-test.yml",),
            "include_pr_template": (".github/pull_request_template.md",),
            "include_issue_templates": (
                ".github/ISSUE_TEMPLATE/bug_report.md",
                ".github/ISSUE_TEMPLATE/feature_request.md",
            ),
            "include_codeowners": (".github/CODEOWNERS",),
            "include_claude_files": (
                "CLAUDE.md",
                ".mcp.json",
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
