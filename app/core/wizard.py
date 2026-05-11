"""Wizard engine for interactive CLI generation flow.

Provides a step-based wizard that prompts the user for scaffold configuration
values with validation, default pre-fill, and save-as-defaults support.
"""

from collections.abc import Callable, Iterator
from typing import Any

from app.core.user_prefs import UserPreferences

# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------


class WizardStep:
    """One step in the interactive generation wizard."""

    def __init__(
        self,
        key: str,
        prompt: str,
        validator: Callable[[str], str] | None = None,
        default: Any = None,
        skip_on_default: bool = False,
        choices: list[str] | None = None,
    ) -> None:
        self.key = key
        self.prompt = prompt
        self.validator = validator
        self.default = default
        self.skip_on_default = skip_on_default
        self.choices = choices


# ---------------------------------------------------------------------------
# Step collection
# ---------------------------------------------------------------------------


def validate_repo_name(value: str) -> str:
    """Validate a repository name string."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("repo_name must not be empty or whitespace")
    if any(c in stripped for c in ("/", "\\", "\0")):
        raise ValueError("repo_name must not contain path separators")
    return stripped


def validate_preset(value: str, allowed: list[str]) -> str:
    """Validate that value is one of the allowed presets."""
    if value not in allowed:
        raise ValueError(f"preset must be one of: {', '.join(allowed)}")
    return value


def validate_bool(value: str) -> str:
    """Validate yes/no input, normalise to yes/no."""
    if value.strip().lower() not in ("yes", "no"):
        raise ValueError("answer must be 'yes' or 'no'")
    return value.strip().lower()


def _resolve_default_value(
    step: WizardStep, prefs: UserPreferences | None
) -> str | None:
    """Extract the pre-fill string default from prefs for *step*, if any."""
    if prefs is None or step.default is None:
        return None
    field = step.default
    if not isinstance(field, str) or not hasattr(prefs, field):
        return None
    val = getattr(prefs, field, "")
    return str(val) if val else None


def _handle_empty_input(step: WizardStep, default_val: str | None) -> str:
    """Resolve the empty-input branch: default → skip-signal → raise."""
    if default_val is not None:
        return default_val
    if step.skip_on_default:
        return step.key  # signal skip
    raise ValueError(f"{step.prompt} cannot be empty")


def collect_wizard_input(
    step: WizardStep,
    inputs: Iterator[str] | None = None,
    prefs: UserPreferences | None = None,
) -> str:
    """Collect one piece of input with validation and pre-fill.

    Returns the validated input. Raises ValueError if the field is required
    and the user supplied no value (and no default is configured).
    """
    default_val = _resolve_default_value(step, prefs)
    display = f" [{default_val}]" if default_val is not None else ""

    while True:
        if inputs is not None:
            raw = next(inputs)
        else:
            raw = input(step.prompt + display + ": ")  # noqa: T201

        if raw.strip() == "":
            return _handle_empty_input(step, default_val)

        if step.validator is None:
            return raw.strip()
        try:
            return step.validator(raw)
        except ValueError:
            continue


def _collect_bool_input(
    step: WizardStep,
    inputs: Iterator[str],
    default: bool,
) -> bool:
    """Collect a yes/no boolean with pre-fill from prefs.

    The default value is passed as a string ('yes'/'no') so collect_wizard_input
    can display it. Returns the normalised boolean.
    """
    value = collect_wizard_input(step, inputs)
    if value == "yes":
        return True
    if value == "no":
        return False
    # skip signal
    return not default  # skip inverts the default


def collect_interactive_wizard(
    steps: list[WizardStep],
    inputs: Iterator[str] | None = None,
    prefs: UserPreferences | None = None,
) -> dict[str, Any]:
    """Run the full interactive wizard, collecting all step values.

    Returns a flat dict of key -> value suitable for passing to RepoConfig.

    Args:
        steps: Ordered list of wizard steps to execute.
        inputs: Canned input for automated testing.
        prefs: User preferences for pre-fill.

    Returns:
        Dictionary mapping step keys to their collected values.
    """
    if prefs is None:
        prefs = UserPreferences()
    results: dict[str, Any] = {}

    for step in steps:
        value = collect_wizard_input(step, inputs, prefs)

        # Handle skip signals — key == value means user accepted default
        if value == step.key:
            if step.default is not None:
                results[step.key] = step.default
            continue

        results[step.key] = value

    return results


# ---------------------------------------------------------------------------
# Preset helpers
# ---------------------------------------------------------------------------

VALID_PRESETS = ("python_basic", "python_desktop", "full_agentic")


def _build_wizard_steps(
    manual_steps: bool = False,
) -> list[WizardStep]:
    """Build the standard wizard step list for generate --interactive.

    When *manual_steps* is True the feature-toggle steps are included so the
    user can pick individual options; otherwise only the top-3 required fields
    are asked and the toggles default to False.
    """
    steps: list[WizardStep] = [
        WizardStep(
            key="repo_name",
            prompt="Repository name",
            validator=validate_repo_name,
            default="author_name",
        ),
        WizardStep(
            key="preset",
            prompt="Preset",
            validator=lambda v: validate_preset(v, list(VALID_PRESETS)),
            default="default_preset",
            choices=list(VALID_PRESETS),
        ),
        WizardStep(
            key="output",
            prompt="Output directory",
            default="default_output_dir",
        ),
    ]

    if manual_steps:
        steps.extend(
            [
                WizardStep(
                    key="include_precommit",
                    prompt="Include pre-commit?",
                    default="yes",
                    skip_on_default=True,
                ),
                WizardStep(
                    key="include_ci",
                    prompt="Include CI workflow?",
                    default="no",
                    skip_on_default=True,
                ),
                WizardStep(
                    key="include_pr_template",
                    prompt="Include PR template?",
                    default="no",
                    skip_on_default=True,
                ),
                WizardStep(
                    key="include_issue_templates",
                    prompt="Include issue templates?",
                    default="no",
                    skip_on_default=True,
                ),
                WizardStep(
                    key="include_codeowners",
                    prompt="Include CODEOWNERS?",
                    default="no",
                    skip_on_default=True,
                ),
                WizardStep(
                    key="include_claude_files",
                    prompt="Include Claude Code files?",
                    default="no",
                    skip_on_default=True,
                ),
            ]
        )

    return steps
