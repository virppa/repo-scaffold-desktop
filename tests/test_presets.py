import pytest

from app.core.presets import Preset, get_preset

EXPECTED_TOGGLE_KEYS = {
    "include_precommit",
    "include_ci",
    "include_pr_template",
    "include_issue_templates",
    "include_codeowners",
    "include_claude_files",
}

ALL_PRESET_NAMES = ("python_basic", "python_desktop", "full_agentic")


def test_get_preset_returns_preset_instance():
    result = get_preset("python_basic")
    assert isinstance(result, Preset)


def test_get_preset_returns_correct_preset():
    result = get_preset("python_basic")
    assert result.name == "python_basic"


def test_get_preset_unknown_name_raises_value_error():
    with pytest.raises(ValueError, match="Unknown preset"):
        get_preset("nonexistent")


def test_get_preset_error_message_lists_available_presets():
    with pytest.raises(ValueError, match="python_basic"):
        get_preset("nonexistent")


def test_python_basic_has_required_files():
    preset = get_preset("python_basic")
    assert len(preset.required_files) > 0


def test_python_basic_optional_files_keys_match_toggles():
    preset = get_preset("python_basic")
    assert set(preset.optional_files.keys()) == EXPECTED_TOGGLE_KEYS


def test_python_desktop_defined():
    preset = get_preset("python_desktop")
    assert preset.name == "python_desktop"


def test_full_agentic_defined():
    preset = get_preset("full_agentic")
    assert preset.name == "full_agentic"


def test_all_presets_have_non_empty_names():
    for name in ALL_PRESET_NAMES:
        preset = get_preset(name)
        assert preset.name.strip() != ""


def test_all_presets_have_non_empty_descriptions():
    for name in ALL_PRESET_NAMES:
        preset = get_preset(name)
        assert preset.description.strip() != ""


def test_all_presets_have_required_files():
    for name in ALL_PRESET_NAMES:
        preset = get_preset(name)
        assert len(preset.required_files) > 0


def test_preset_is_immutable():
    preset = get_preset("python_basic")
    with pytest.raises((AttributeError, TypeError)):
        preset.name = "modified"


def test_full_agentic_has_skills_config():
    preset = get_preset("full_agentic")
    assert preset.skills_source == "github:virppa/repo-scaffold-skills"
    assert preset.skills_version == "v1.0.0"


def test_non_agentic_presets_have_no_skills_config():
    for name in ("python_basic", "python_desktop"):
        preset = get_preset(name)
        assert preset.skills_source is None
        assert preset.skills_version is None
