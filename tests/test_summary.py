"""Tests for app.core.summary — manual-steps renderer."""

from __future__ import annotations

import pytest

from app.core.summary import MANUAL_STEPS_MAP, ManualStep, render_summary

# ---------------------------------------------------------------------------
# MANUAL_STEPS_MAP shape
# ---------------------------------------------------------------------------


def test_manual_steps_map_keys() -> None:
    """The map exposes exactly the three feature keys."""
    assert set(MANUAL_STEPS_MAP.keys()) == {"linear_mcp", "github_repo", "git_push"}


def test_manual_steps_map_entries_are_dataclass() -> None:
    """Each entry is a ManualStep with title + command set."""
    for key, step in MANUAL_STEPS_MAP.items():
        assert isinstance(step, ManualStep), key
        assert step.title, key
        assert step.command, key


# ---------------------------------------------------------------------------
# render_summary — Done automatically section
# ---------------------------------------------------------------------------


def _render_minimal(**overrides: object) -> str:
    """Render with defaults; pass overrides as kwargs."""
    defaults: dict[str, object] = {
        "files_written": 0,
        "output_path": "./out",
        "git_init_done": False,
        "precommit_installed": False,
        "linear_mcp_generated": False,
        "github_repo_created": False,
        "git_pushed": False,
    }
    defaults.update(overrides)
    return render_summary(**defaults)  # type: ignore[arg-type]


def test_done_section_files_written_count() -> None:
    out = _render_minimal(files_written=18, output_path="./my-repo")
    assert "18 files written to ./my-repo" in out


def test_done_section_git_init_when_done() -> None:
    out = _render_minimal(git_init_done=True)
    assert "git init completed" in out


def test_done_section_git_init_omitted_when_not_done() -> None:
    out = _render_minimal(git_init_done=False)
    assert "git init completed" not in out


def test_done_section_precommit_when_installed() -> None:
    out = _render_minimal(precommit_installed=True)
    assert "pre-commit installed" in out


def test_done_section_precommit_omitted_when_not_installed() -> None:
    out = _render_minimal(precommit_installed=False)
    assert "pre-commit installed" not in out


# ---------------------------------------------------------------------------
# render_summary — Do these manually section
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("linear_mcp_generated", "expected_present"),
    [(True, True), (False, False)],
)
def test_linear_mcp_step_visibility(
    linear_mcp_generated: bool, expected_present: bool
) -> None:
    """The Linear MCP step shows iff include_linear_mcp was set."""
    out = _render_minimal(linear_mcp_generated=linear_mcp_generated)
    assert (MANUAL_STEPS_MAP["linear_mcp"].title in out) is expected_present


@pytest.mark.parametrize(
    ("github_repo_created", "expected_present"),
    [(False, True), (True, False)],
)
def test_github_repo_step_visibility(
    github_repo_created: bool, expected_present: bool
) -> None:
    """The GitHub repo step shows iff the repo was NOT created automatically."""
    out = _render_minimal(github_repo_created=github_repo_created)
    assert (MANUAL_STEPS_MAP["github_repo"].title in out) is expected_present


@pytest.mark.parametrize(
    ("git_pushed", "expected_present"),
    [(False, True), (True, False)],
)
def test_git_push_step_visibility(git_pushed: bool, expected_present: bool) -> None:
    """The git-push step shows iff push was NOT done automatically."""
    out = _render_minimal(git_pushed=git_pushed)
    assert (MANUAL_STEPS_MAP["git_push"].title in out) is expected_present


def test_manual_section_header_omitted_when_no_steps() -> None:
    """If all features were handled automatically, the manual section disappears."""
    out = _render_minimal(
        linear_mcp_generated=False,
        github_repo_created=True,
        git_pushed=True,
    )
    assert "Do these manually:" not in out


def test_manual_section_header_present_when_any_step() -> None:
    out = _render_minimal(linear_mcp_generated=True)
    assert "Do these manually:" in out


# ---------------------------------------------------------------------------
# Full matrix — feature on/off combinations across the three flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("linear_mcp_generated", [False, True])
@pytest.mark.parametrize("github_repo_created", [False, True])
@pytest.mark.parametrize("git_pushed", [False, True])
def test_feature_matrix(
    linear_mcp_generated: bool, github_repo_created: bool, git_pushed: bool
) -> None:
    """Every combination of feature flags renders the expected step set."""
    out = _render_minimal(
        linear_mcp_generated=linear_mcp_generated,
        github_repo_created=github_repo_created,
        git_pushed=git_pushed,
    )
    expected_steps = []
    if linear_mcp_generated:
        expected_steps.append("linear_mcp")
    if not github_repo_created:
        expected_steps.append("github_repo")
    if not git_pushed:
        expected_steps.append("git_push")

    for key in expected_steps:
        assert MANUAL_STEPS_MAP[key].title in out, key

    for key in set(MANUAL_STEPS_MAP) - set(expected_steps):
        assert MANUAL_STEPS_MAP[key].title not in out, key


# ---------------------------------------------------------------------------
# Formatting / no-ANSI / structure
# ---------------------------------------------------------------------------


def test_no_ansi_escape_sequences() -> None:
    """Output is plain ASCII — no terminal colour codes."""
    out = _render_minimal(
        files_written=5,
        git_init_done=True,
        precommit_installed=True,
        linear_mcp_generated=True,
    )
    assert "\x1b" not in out  # no escape character anywhere


def test_done_marker_uses_plain_ascii() -> None:
    out = _render_minimal(files_written=1)
    assert "[done]" in out


def test_manual_marker_uses_plain_ascii() -> None:
    out = _render_minimal(linear_mcp_generated=True)
    assert "[manual]" in out


def test_command_lines_included_for_manual_steps() -> None:
    out = _render_minimal(linear_mcp_generated=True)
    assert MANUAL_STEPS_MAP["linear_mcp"].command in out
