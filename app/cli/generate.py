"""Scaffold generation flow: `python -m app.cli generate ...`."""

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from app.core.config import RepoConfig
from app.core.generator import generate
from app.core.post_setup import (
    _parse_repo_full_name,
    configure_github_repo,
    create_github_repo,
    delete_github_repo,
    fetch_skills,
    run_git_init,
    run_initial_push,
    run_precommit_install,
)
from app.core.presets import get_preset
from app.core.user_prefs import PrefsStore, UserPreferences
from app.core.wizard import (
    WizardStep,
    collect_interactive_wizard,
    validate_repo_name,
)


def _build_config(args: argparse.Namespace) -> RepoConfig:
    include_linear_mcp: bool = args.linear_mcp
    if include_linear_mcp is None:
        include_linear_mcp = get_preset(args.preset).context_defaults.get(
            "include_linear_mcp", False
        )
    return RepoConfig(
        repo_name=args.repo_name,
        preset=args.preset,
        include_precommit=args.pre_commit,
        include_ci=args.ci,
        include_pr_template=args.pr_template,
        include_issue_templates=args.issue_templates,
        include_codeowners=args.codeowners,
        include_claude_files=args.claude_files,
        include_linear_mcp=include_linear_mcp,
        include_playwright=args.playwright,
        git_init=args.git_init,
        install_precommit=args.install_precommit,
    )


def _run_interactive(args: argparse.Namespace) -> int:
    """Run the interactive wizard for generate --interactive."""
    from app.core.wizard import validate_bool

    steps = [
        WizardStep(
            key="repo_name",
            prompt="Repository name",
            validator=validate_repo_name,
            default="author_name",
        ),
        WizardStep(
            key="preset",
            prompt="Preset (python_basic | python_desktop | full_agentic)",
            default="default_preset",
        ),
        WizardStep(
            key="output",
            prompt="Output directory",
            default="default_output_dir",
        ),
    ]

    if args.manual_steps:
        steps.extend(
            [
                WizardStep(
                    key="include_precommit",
                    prompt="Include pre-commit config? [yes/no]",
                    validator=validate_bool,
                ),
                WizardStep(
                    key="include_ci",
                    prompt="Include CI workflow? [yes/no]",
                    validator=validate_bool,
                ),
                WizardStep(
                    key="include_pr_template",
                    prompt="Include PR template? [yes/no]",
                    validator=validate_bool,
                ),
                WizardStep(
                    key="include_issue_templates",
                    prompt="Include issue templates? [yes/no]",
                    validator=validate_bool,
                ),
                WizardStep(
                    key="include_codeowners",
                    prompt="Include CODEOWNERS? [yes/no]",
                    validator=validate_bool,
                ),
                WizardStep(
                    key="include_claude_files",
                    prompt="Include Claude Code files? [yes/no]",
                    validator=validate_bool,
                ),
            ]
        )

    # Load preferences if prefill is enabled
    prefs: UserPreferences | None = None
    if args.prefill or args.save_defaults:
        prefs = PrefsStore.load()

    # Collect wizard input
    results = collect_interactive_wizard(steps, inputs=None, prefs=prefs)

    try:
        config = RepoConfig(
            repo_name=results["repo_name"],
            preset=results["preset"],
            include_precommit=results.get("include_precommit", False),
            include_ci=results.get("include_ci", False),
            include_pr_template=results.get("include_pr_template", False),
            include_issue_templates=results.get("include_issue_templates", False),
            include_codeowners=results.get("include_codeowners", False),
            include_claude_files=results.get("include_claude_files", False),
            include_linear_mcp=get_preset(results["preset"]).context_defaults.get(
                "include_linear_mcp", False
            ),
            git_init=args.git_init,
            install_precommit=args.install_precommit,
        )
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Save defaults if requested
    if args.save_defaults and prefs is not None:
        updated = prefs.model_copy(
            update={
                "author_name": results.get("author_name", prefs.author_name),
                "github_username": results.get(
                    "github_username", prefs.github_username
                ),
            }
        )
        PrefsStore.save(updated)
        print("Saved defaults.", file=sys.stderr)

    # Execute the non-interactive generate flow
    try:
        _render_and_report(config, Path(str(results["output"])))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        _fetch_skills_for_preset(Path(str(results["output"])), config)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        _run_post_setup(config, Path(str(results["output"])))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _render_and_report(config: RepoConfig, output: Path) -> list[str]:
    written = generate(config, output)
    for path in written:
        print(f"✓ {path}")
    return written


def _fetch_skills_for_preset(output: Path, config: RepoConfig) -> list[str]:
    preset = get_preset(config.preset)
    if preset.skills_source is None or preset.skills_version is None:
        return []
    ctx = config.model_dump()
    skills_context = (
        {k: ctx[k] for k in preset.skills_context_fields}
        if preset.skills_context_fields
        else None
    )
    skills_written = fetch_skills(
        output,
        skills_source=preset.skills_source,
        skills_version=preset.skills_version,
        context=skills_context,
    )
    for path in skills_written:
        print(f"✓ {path}")
    return skills_written


def _run_post_setup(config: RepoConfig, output: Path) -> None:
    if config.git_init:
        run_git_init(output)
        print("✓ git init")
    if config.install_precommit:
        run_precommit_install(output)
        print("✓ pre-commit install")


def _run_github_create(config: RepoConfig, args: argparse.Namespace) -> str | None:
    github_private = not args.public
    clone_url = create_github_repo(
        repo_name=config.repo_name,
        private=github_private,
    )
    print(f"✓ Created GitHub repo: {clone_url}")
    return clone_url


def _run_initial_push(output: Path, push_url: str) -> None:
    prefs = PrefsStore.load()
    run_initial_push(output, push_url, prefs)
    print(f"✓ Pushed initial commit to {push_url}")


def _validate_generate_args(args: argparse.Namespace) -> bool:
    """Return True if required args are present (or interactive mode)."""
    if args.interactive:
        return True
    if not args.preset or not args.repo_name or not args.output:
        print(
            "error: the following arguments are required: "
            "--preset, --repo-name, --output",
            file=sys.stderr,
        )
        return False
    return True


def _run_github_phase(
    args: argparse.Namespace, config: RepoConfig
) -> tuple[int, str | None, str | None]:
    """Run optional GitHub create + configure + push.

    Returns (exit_code, clone_url, repo_full_name). exit_code is 0 on success.
    """
    clone_url: str | None = None
    repo_full_name: str | None = None
    rollback = not args.no_rollback_on_failure

    if args.github_create:
        try:
            clone_url = _run_github_create(config, args)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1, None, None

    if clone_url is not None:
        repo_full_name = _parse_repo_full_name(clone_url)

    if repo_full_name is not None and clone_url is not None:
        try:
            configure_github_repo(repo_full_name, config.preset, config.include_ci)
            print("✓ Configured GitHub repository settings")
        except RuntimeError as exc:
            if rollback:
                delete_github_repo(repo_full_name)
            print(f"Error: {exc}", file=sys.stderr)
            return 1, None, None

    if args.git_push:
        push_url: str | None = clone_url if clone_url is not None else args.remote_url
        if push_url is None:
            print("Error: no remote URL specified", file=sys.stderr)
            return 1, None, None
        try:
            _run_initial_push(args.output, push_url)
        except RuntimeError as exc:
            if rollback and repo_full_name is not None:
                delete_github_repo(repo_full_name)
            print(f"Error: {exc}", file=sys.stderr)
            return 1, None, None

    return 0, clone_url, repo_full_name


def _execute_generation_pipeline(config: RepoConfig, args: argparse.Namespace) -> int:
    """Run render → fetch skills → post-setup → GitHub phase. Returns rc."""
    try:
        _render_and_report(config, args.output)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        _fetch_skills_for_preset(args.output, config)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        _run_post_setup(config, args.output)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    exit_code, _clone_url, _repo_full_name = _run_github_phase(args, config)
    return exit_code


def _run_generate(args: argparse.Namespace) -> int:
    """Top-level dispatcher for `generate` subcommand."""
    if args.interactive:
        return _run_interactive(args)

    if not _validate_generate_args(args):
        return 1

    try:
        config = _build_config(args)
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return _execute_generation_pipeline(config, args)
