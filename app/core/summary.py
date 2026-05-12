"""Manual-steps summary renderer for scaffold generation.

Printed after a successful `python -m app.cli generate --interactive` so the
operator knows what was done and what still needs manual follow-up. Driven by
a declarative map keyed on feature name — no hardcoded conditionals in the
formatter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ManualStep:
    """A manual follow-up step the operator must perform after scaffold generation."""

    title: str
    command: str


# Declarative map keyed on feature name. Each entry describes the operator-side
# step that becomes relevant for that feature. The renderer below decides which
# steps to include based on explicit booleans the caller passes in — the map
# itself is just the catalogue.
MANUAL_STEPS_MAP: Mapping[str, ManualStep] = {
    "linear_mcp": ManualStep(
        title="Authenticate Linear MCP",
        command="Run /mcp in Claude Code (the .mcp.json is already configured)",
    ),
    "github_repo": ManualStep(
        title="Create GitHub repository",
        command="gh repo create <repo-name> --private",
    ),
    "git_push": ManualStep(
        title="Push initial commit",
        command="cd <repo-name> && git push -u origin main",
    ),
}


_STATUS_DONE = "[done]"
_STATUS_MANUAL = "[manual]"


def render_summary(
    *,
    files_written: int,
    output_path: str,
    git_init_done: bool,
    precommit_installed: bool,
    linear_mcp_generated: bool,
    github_repo_created: bool,
    git_pushed: bool,
) -> str:
    """Render a two-section generation summary.

    "Done automatically" reflects what the tool just did (files written, git
    init, pre-commit install).

    "Do these manually" lists features that need operator follow-up:
    `linear_mcp` shows if the Linear MCP file was generated; `github_repo`
    and `git_push` show when those weren't done automatically (i.e. when the
    operator didn't pass the corresponding CLI flag — which is always the case
    in interactive mode today).

    Plain ASCII output, no ANSI escape sequences.
    """
    done_items: list[str] = [f"{files_written} files written to {output_path}"]
    if git_init_done:
        done_items.append("git init completed")
    if precommit_installed:
        done_items.append("pre-commit installed")

    feature_flags = {
        "linear_mcp": linear_mcp_generated,
        "github_repo": not github_repo_created,
        "git_push": not git_pushed,
    }
    manual_keys = [key for key, show in feature_flags.items() if show]

    lines: list[str] = ["", "Done automatically:"]
    lines.extend(f"  {_STATUS_DONE} {item}" for item in done_items)

    if manual_keys:
        lines.append("")
        lines.append("Do these manually:")
        for key in manual_keys:
            step = MANUAL_STEPS_MAP[key]
            lines.append(f"  {_STATUS_MANUAL} {step.title}")
            lines.append(f"           {step.command}")

    return "\n".join(lines)
