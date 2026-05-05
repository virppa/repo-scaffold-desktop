"""Tests for the wizard engine (app/core/wizard.py)."""

from unittest.mock import MagicMock, patch

import pytest

from app.core.user_prefs import UserPreferences
from app.core.wizard import (
    VALID_PRESETS,
    WizardStep,
    collect_interactive_wizard,
    collect_wizard_input,
    validate_bool,
    validate_preset,
    validate_repo_name,
)

# ---------------------------------------------------------------------------
# validate_repo_name
# ---------------------------------------------------------------------------


class TestValidateRepoName:
    """Good and bad input for repository name validation."""

    def test_good_name_returns_stripped(self):
        assert validate_repo_name("my-repo") == "my-repo"

    def test_whitespace_stripped(self):
        assert validate_repo_name("  my-repo  ") == "my-repo"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_repo_name("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            validate_repo_name("   ")

    def test_forward_slash_raises(self):
        with pytest.raises(ValueError, match="path separators"):
            validate_repo_name("foo/bar")

    def test_backslash_raises(self):
        with pytest.raises(ValueError, match="path separators"):
            validate_repo_name("foo\\bar")

    def test_null_byte_raises(self):
        with pytest.raises(ValueError, match="path separators"):
            validate_repo_name("foo\0bar")


# ---------------------------------------------------------------------------
# validate_preset
# ---------------------------------------------------------------------------


class TestValidatePreset:
    """Good and bad input for preset validation."""

    def test_valid_preset_accepted(self):
        for preset in VALID_PRESETS:
            assert validate_preset(preset, list(VALID_PRESETS)) == preset

    def test_invalid_preset_raises(self):
        with pytest.raises(ValueError, match="python_basic"):
            validate_preset("invalid", list(VALID_PRESETS))

    def test_error_lists_allowed_values(self):
        with pytest.raises(ValueError, match="full_agentic"):
            validate_preset("bad", list(VALID_PRESETS))


# ---------------------------------------------------------------------------
# validate_bool
# ---------------------------------------------------------------------------


class TestValidateBool:
    """Yes/no validation."""

    def test_yes_lower(self):
        assert validate_bool("yes") == "yes"

    def test_no_lower(self):
        assert validate_bool("no") == "no"

    def test_yes_upper(self):
        assert validate_bool("YES") == "yes"

    def test_no_upper(self):
        assert validate_bool("NO") == "no"

    def test_mixed_case(self):
        assert validate_bool("Yes") == "yes"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="must be"):
            validate_bool("maybe")

    def test_numeric_raises(self):
        with pytest.raises(ValueError, match="must be"):
            validate_bool("1")


# ---------------------------------------------------------------------------
# collect_wizard_input — good/bad input
# ---------------------------------------------------------------------------


class TestCollectWizardInput:
    """Input collection with canned answers via iterator."""

    def test_accepts_valid_input(self):
        step = WizardStep(key="name", prompt="Name")
        value = collect_wizard_input(step, iter(["  Alice  "]))
        assert value == "Alice"

    def test_empty_with_prefs_default_uses_prefill(self):
        """Empty input with a matching UserPreference field uses the pref value."""
        prefs = UserPreferences(author_name="Bob")
        step = WizardStep(key="name", prompt="Name", default="author_name")
        value = collect_wizard_input(step, iter([""]), prefs)
        assert value == "Bob"

    def test_empty_no_default_raises(self):
        step = WizardStep(key="name", prompt="Name")
        with pytest.raises(ValueError, match="cannot be empty"):
            collect_wizard_input(step, iter([""]))

    def test_validator_retries_on_invalid(self):
        """Invalid input keeps retrying until valid — empty goes through pre-path."""
        step = WizardStep(
            key="name",
            prompt="Name",
            validator=lambda v: (
                v.strip() if v.strip() else (_ for _ in ()).throw(ValueError("empty"))
            ),
        )
        # Empty without default raises; non-empty passes validator
        with pytest.raises(ValueError, match="cannot be empty"):
            collect_wizard_input(step, iter([""]))
        # Then a valid value works
        value = collect_wizard_input(step, iter(["Alice"]))
        assert value == "Alice"

    def test_empty_skips_returns_step_key(self):
        """Empty input with skip_on_default returns step key."""
        step = WizardStep(
            key="include_precommit",
            prompt="Pre-commit?",
            default="yes",
            skip_on_default=True,
        )
        value = collect_wizard_input(step, iter([""]))
        # Step key returned to signal "accept default" — caller handles it
        assert value == "include_precommit"

    def test_validator_rejected_then_valid(self):
        """Bad non-empty input keeps looping; valid input eventually accepted."""
        step = WizardStep(
            key="repo_name",
            prompt="Repo name",
            validator=validate_repo_name,
        )
        value = collect_wizard_input(step, iter(["bad/name", "myrepo"]))
        assert value == "myrepo"


# ---------------------------------------------------------------------------
# collect_wizard_input — default pre-fill
# ---------------------------------------------------------------------------


class TestCollectWizardInputPrefill:
    """Default pre-fill from UserPreferences."""

    def test_prefill_shows_default_in_prompt(self):
        prefs = UserPreferences(author_name="Alice")
        step = WizardStep(
            key="name",
            prompt="Name",
            default="author_name",
        )
        mock_input = MagicMock(return_value="")
        with patch("app.core.wizard.input", mock_input):
            collect_wizard_input(step, None, prefs)
        mock_input.assert_called_once()
        call_arg = mock_input.call_args[0][0]
        assert "[Alice]" in call_arg

    def test_empty_accepts_default(self):
        prefs = UserPreferences(author_name="Alice")
        step = WizardStep(
            key="name",
            prompt="Name",
            default="author_name",
        )
        value = collect_wizard_input(step, iter([""]), prefs)
        assert value == "Alice"

    def test_empty_no_prefs_no_default_raises(self):
        """No prefs + no default + no skip = ValueError."""
        step = WizardStep(key="name", prompt="Name")
        with pytest.raises(ValueError, match="cannot be empty"):
            collect_wizard_input(step, iter([""]), UserPreferences())


# ---------------------------------------------------------------------------
# collect_wizard_input — skip-on-default
# ---------------------------------------------------------------------------


class TestSkipOnDefault:
    """skip_on_default = True means empty input returns the step key."""

    def test_empty_returns_step_key(self):
        step = WizardStep(
            key="include_precommit",
            prompt="Pre-commit?",
            default="yes",
            skip_on_default=True,
        )
        value = collect_wizard_input(step, iter([""]))
        assert value == "include_precommit"

    def test_empty_with_prefs_returns_step_key_not_default(self):
        """When skip_on_default is True, empty returns step key even with prefs."""
        prefs = UserPreferences(author_name="Alice")
        step = WizardStep(
            key="include_ci",
            prompt="CI?",
            default="no",
            skip_on_default=True,
        )
        value = collect_wizard_input(step, iter([""]), prefs)
        assert value == "include_ci"

    def test_skip_on_default_with_no_default_value(self):
        """skip_on_default with no default value returns the key as-is."""
        step = WizardStep(
            key="feature_flag",
            prompt="Feature?",
            skip_on_default=True,
        )
        value = collect_wizard_input(step, iter([""]))
        assert value == "feature_flag"


# ---------------------------------------------------------------------------
# collect_interactive_wizard
# ---------------------------------------------------------------------------


class TestCollectInteractiveWizard:
    """Full wizard run with multiple steps."""

    def test_all_steps_collected(self):
        steps = [
            WizardStep(key="a", prompt="A"),
            WizardStep(key="b", prompt="B"),
            WizardStep(key="c", prompt="C"),
        ]
        results = collect_interactive_wizard(steps, iter(["1", "2", "3"]))
        assert results == {"a": "1", "b": "2", "c": "3"}

    def test_skip_signal_uses_default(self):
        """Skip signal (key == value) falls through to default."""
        steps = [
            WizardStep(
                key="include_precommit",
                prompt="Pre-commit?",
                default="yes",
                skip_on_default=True,
            ),
        ]
        results = collect_interactive_wizard(steps, iter(["include_precommit"]))
        assert results["include_precommit"] == "yes"

    def test_none_prefs_creates_empty_prefs(self):
        """Passing None prefs should not crash — creates empty UserPreferences."""
        steps = [WizardStep(key="a", prompt="A")]
        results = collect_interactive_wizard(steps, iter(["v"]), None)
        assert results == {"a": "v"}

    def test_values_overwrite_in_results(self):
        """Later steps overwrite earlier ones if they share a key (edge case)."""
        steps = [
            WizardStep(key="a", prompt="A", default="x"),
            WizardStep(key="a", prompt="A2", default="y"),
        ]
        results = collect_interactive_wizard(steps, iter(["v1", "v2"]))
        assert results["a"] == "v2"

    def test_empty_input_with_default_prefills(self):
        """Empty input for a step with default uses that default in results."""
        steps = [
            WizardStep(key="preset", prompt="Preset", default="default_preset"),
        ]
        results = collect_interactive_wizard(
            steps,
            iter([""]),
            UserPreferences(default_preset="full_agentic"),
        )
        assert results["preset"] == "full_agentic"


# ---------------------------------------------------------------------------
# WizardStep
# ---------------------------------------------------------------------------


class TestWizardStep:
    """WizardStep construction and attribute access."""

    def test_required_fields(self):
        step = WizardStep(key="k", prompt="P")
        assert step.key == "k"
        assert step.prompt == "P"
        assert step.validator is None
        assert step.default is None
        assert step.skip_on_default is False
        assert step.choices is None

    def test_all_fields(self):
        step = WizardStep(
            key="k",
            prompt="P",
            validator=lambda v: v,
            default="d",
            skip_on_default=True,
            choices=["a", "b"],
        )
        assert step.key == "k"
        assert step.validator is not None
        assert step.default == "d"
        assert step.skip_on_default is True
        assert step.choices == ["a", "b"]


# ---------------------------------------------------------------------------
# Edge cases for collect_wizard_input
# ---------------------------------------------------------------------------


class TestCollectWizardInputEdgeCases:
    """Edge cases in input collection."""

    def test_iterator_exhausted_raises_stopiteration(self):
        step = WizardStep(key="k", prompt="P")
        with pytest.raises(StopIteration):
            collect_wizard_input(step, iter([]))

    def test_validator_can_accept_value_as_is(self):
        """Validator that returns the value unchanged works."""

        def identity(v):
            return v

        step = WizardStep(key="k", prompt="P", validator=identity)
        value = collect_wizard_input(step, iter(["raw_input"]))
        assert value == "raw_input"

    def test_validator_can_normalize_value(self):
        step = WizardStep(key="k", prompt="P", validator=validate_bool)
        value = collect_wizard_input(step, iter(["YES"]))
        assert value == "yes"
