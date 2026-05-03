import argparse
import getpass
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

from dotenv import load_dotenv
from keyring.errors import KeyringError, NoKeyringError
from pydantic import ValidationError

from app.core.config import RepoConfig
from app.core.credentials import cli_delete_token, save_token
from app.core.generator import generate
from app.core.metrics import MetricsStore
from app.core.post_setup import (
    configure_github_repo,
    create_github_repo,
    fetch_skills,
    run_git_init,
    run_initial_push,
    run_precommit_install,
)
from app.core.presets import _PRESETS, get_preset
from app.core.user_prefs import PrefsStore, UserPreferences

_PREFS_KEYS = set(UserPreferences.model_fields)
_KEY_TO_FIELD = {k.replace("_", "-"): k for k in _PREFS_KEYS}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scaffold",
        description="Generate a repository scaffold from a preset.",
    )
    sub = parser.add_subparsers(dest="command")

    cfg = sub.add_parser("config", help="Manage user preferences.")
    cfg_sub = cfg.add_subparsers(dest="config_cmd")

    cfg_sub.add_parser("get", help="Print current preferences.")

    cfg_set = cfg_sub.add_parser("set", help="Set a preference value.")
    cfg_set.add_argument(
        "key",
        choices=set(sorted(_KEY_TO_FIELD)) | {"github-token"},
        help="Preference key (use hyphens, e.g. author-name).",
    )
    cfg_set.add_argument("value", nargs="?", default=None, help="Value to store.")

    cfg_del = cfg_sub.add_parser("delete", help="Delete a credential.")
    cfg_del.add_argument(
        "key",
        choices=set(sorted(_KEY_TO_FIELD)) | {"github-token"},
        help="Credential key to delete.",
    )

    gen = sub.add_parser("generate", help="Generate scaffold files.")
    gen.add_argument(
        "--preset",
        required=True,
        choices=list(_PRESETS),
        help="Preset to use.",
    )
    gen.add_argument("--repo-name", required=True, help="Repository name.")
    gen.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output directory.",
    )
    gen.add_argument(
        "--pre-commit", action="store_true", help="Include pre-commit config."
    )
    gen.add_argument("--ci", action="store_true", help="Include CI workflow.")
    gen.add_argument("--pr-template", action="store_true", help="Include PR template.")
    gen.add_argument(
        "--issue-templates", action="store_true", help="Include issue templates."
    )
    gen.add_argument(
        "--codeowners", action="store_true", help="Include CODEOWNERS file."
    )
    gen.add_argument(
        "--claude-files", action="store_true", help="Include Claude Code files."
    )
    gen.add_argument(
        "--linear-mcp",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Include Linear MCP server in .mcp.json. "
            "full_agentic preset defaults to True; all others default to False."
        ),
    )
    gen.add_argument(
        "--playwright",
        action="store_true",
        help=(
            "Include Playwright browser-test scaffold (full_agentic only — "
            "for web-facing repos, not PySide6 desktop apps)."
        ),
    )
    gen.add_argument(
        "--git-init", action="store_true", help="Run git init in the output directory."
    )
    gen.add_argument(
        "--install-precommit",
        action="store_true",
        help="Run pre-commit install in the output directory.",
    )
    gen.add_argument(
        "--github-create", action="store_true", help="Create a GitHub repository."
    )
    gen.add_argument(
        "--git-push", action="store_true", help="Push initial commit to remote."
    )
    gen.add_argument(
        "--remote-url",
        default=None,
        help="Remote URL to push to (used with --git-push).",
    )
    github_group = gen.add_mutually_exclusive_group()
    github_group.add_argument(
        "--private", action="store_true", help="Create a private GitHub repository."
    )
    github_group.add_argument(
        "--public", action="store_true", help="Create a public GitHub repository."
    )

    metrics = sub.add_parser("metrics", help="Metrics DB commands.")
    metrics_sub = metrics.add_subparsers(dest="metrics_cmd")
    metrics_sub.add_parser("browse", help="Open metrics DB in Datasette browser UI.")

    watcher = sub.add_parser(
        "watcher", help="Run the local worker orchestrator daemon."
    )
    watcher.add_argument(
        "--worker-mode",
        choices=["cloud", "local", "default"],
        default=None,
        help=(
            "Override implementation_mode for all tickets. "
            "cloud: route to Anthropic API (no ANTHROPIC_BASE_URL injected). "
            "local: route to LiteLLM proxy with qwen3-coder:30b. "
            "default: respect each manifest's implementation_mode. "
            "Also reads WORKER_MODE env var (flag takes precedence)."
        ),
    )
    watcher.add_argument(
        "--max-local-workers",
        type=int,
        default=8,
        help="Maximum concurrent local worker sessions (default: 8).",
    )
    watcher.add_argument(
        "--max-cloud-workers",
        type=int,
        default=3,
        help="Maximum concurrent cloud worker sessions (default: 3).",
    )
    watcher.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help=(
            "Backward-compatible alias: sets both --max-local-workers and "
            "--max-cloud-workers to the same value."
        ),
    )
    watcher.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help=(
            "Set DEBUG level on the watcher's own logger. "
            "Does not affect worker stdout streaming."
        ),
    )
    watcher.add_argument(
        "--worker-verbose",
        action="store_true",
        default=False,
        help=(
            "Stream worker stdout+stderr live to the daemon's stderr, "
            "prefixed with [WOR-NN]. Output is still written to the log file."
        ),
    )
    watcher.add_argument(
        "--no-epic-shutdown",
        action="store_true",
        default=False,
        help=(
            "Keep the watcher running after all current sub-tickets are "
            "processed instead of exiting. Useful for watching new tickets "
            "get added to the epic."
        ),
    )
    watcher.add_argument(
        "--tui",
        action="store_true",
        default=False,
        help=(
            "Show a live rich-based TUI with per-worker status, cost "
            "economics, historical rollups, and PR auto-merge tracking. "
            "Falls back to line-based logging when stderr is piped."
        ),
    )

    return parser


def _run_watcher(args: argparse.Namespace) -> int:
    import logging

    from app.core.watcher import Watcher
    from app.core.watcher.log_format import ColorFormatter

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        ColorFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        handlers=[handler],
    )
    mode = args.worker_mode or os.environ.get("WORKER_MODE", "default")
    max_local = args.max_local_workers
    max_cloud = args.max_cloud_workers
    if args.max_workers is not None:
        max_local = args.max_workers
        max_cloud = args.max_workers
    watcher = Watcher(
        worker_mode=mode,
        max_local_workers=max_local,
        max_cloud_workers=max_cloud,
        worker_verbose=args.worker_verbose,
        no_epic_shutdown=args.no_epic_shutdown,
        tui_mode=args.tui,
    )
    try:
        watcher.run()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_metrics(args: argparse.Namespace) -> int:
    if args.metrics_cmd == "browse":
        db_path = MetricsStore.get_db_path()
        if not db_path.exists():
            print(
                f"Error: metrics DB not found at {db_path}. "
                "Run the watcher at least once to create it.",
                file=sys.stderr,
            )
            return 1
        try:
            subprocess.run(["datasette", str(db_path)], check=False)  # nosec B603 B607
        except FileNotFoundError:
            print(
                "Error: datasette not installed. Run: pip install datasette",
                file=sys.stderr,
            )
            return 1
        return 0

    print("Usage: scaffold metrics {browse}", file=sys.stderr)
    return 1


def _config_get(args: argparse.Namespace) -> int:
    prefs = PrefsStore.load()
    for field, value in prefs.model_dump().items():
        key = field.replace("_", "-")
        print(f"{key}: {value}")
    return 0


def _config_set(args: argparse.Namespace) -> int:
    if args.key == "github-token":
        raw = args.value
        if not raw:
            raw = getpass.getpass(
                "GitHub token: ",
                stream=sys.stderr,
            )
        if not raw:
            print("Error: empty token", file=sys.stderr)
            return 1
        try:
            save_token(raw)
            print("Done.", file=sys.stderr)
        except (NoKeyringError, KeyringError) as exc:
            print(
                f"Error: unable to store token ({type(exc).__name__})",
                file=sys.stderr,
            )
            return 1
        return 0

    field = _KEY_TO_FIELD[args.key]
    prefs = PrefsStore.load()
    raw = args.value
    field_info = UserPreferences.model_fields[field]
    annotation = field_info.annotation
    # Handle Path | None
    if annotation in (Path, "Path | None") or (
        annotation is not None
        and hasattr(annotation, "__args__")
        and Path in annotation.__args__
    ):
        value = Path(raw) if raw else None
    else:
        value = raw
    updated = prefs.model_copy(update={field: value})
    PrefsStore.save(updated)
    print(f"✓ {args.key} = {value}")
    return 0


def _config_delete(args: argparse.Namespace) -> int:
    if args.key == "github-token":
        return cli_delete_token()
    print(
        f"Error: unknown credential '{args.key}'",
        file=sys.stderr,
    )
    return 1


def _run_config(args: argparse.Namespace) -> int:
    if args.config_cmd == "get":
        return _config_get(args)
    if args.config_cmd == "set":
        return _config_set(args)
    if args.config_cmd == "delete":
        return _config_delete(args)

    print("Usage: scaffold config {get,set,delete}", file=sys.stderr)
    return 1


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
    prefs = PrefsStore.load()
    github_private = not args.public
    clone_url = create_github_repo(
        repo_name=config.repo_name,
        prefs=prefs,
        private=github_private,
    )
    print(f"✓ Created GitHub repo: {clone_url}")
    repo_full_name = clone_url.replace("https://github.com/", "").rstrip("/")
    try:
        configure_github_repo(repo_full_name, config.preset, config.include_ci)
        print("✓ Configured GitHub repository settings")
    except RuntimeError as exc:
        print(f"Warning: {exc}", file=sys.stderr)
    return clone_url


def _run_initial_push(output: Path, push_url: str) -> None:
    prefs = PrefsStore.load()
    run_initial_push(output, push_url, prefs)
    print(f"✓ Pushed initial commit to {push_url}")


def _run_generate(args: argparse.Namespace) -> int:
    try:
        config = _build_config(args)
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

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

    clone_url: str | None = None
    if args.github_create:
        try:
            clone_url = _run_github_create(config, args)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.git_push:
        push_url: str | None = clone_url if clone_url is not None else args.remote_url
        if push_url is None:
            print("Error: no remote URL specified", file=sys.stderr)
            return 1
        try:
            _run_initial_push(args.output, push_url)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    # Ensure the terminal can emit UTF-8 (e.g. ✓); no-op on StringIO (pytest capsys).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # nosec B110 — intentional no-op; stdout may not support reconfigure
            pass

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "generate":
        # --git-push implies --git-init
        if getattr(args, "git_push", False):
            args.git_init = True
        # --git-push requires either --github-create or --remote-url
        has_remote = args.github_create or getattr(args, "remote_url", None)
        if getattr(args, "git_push", False) and not has_remote:
            parser.error(
                "argument --git-push: must also specify --github-create or --remote-url"
            )

    if args.command == "config":
        return _run_config(args)

    if args.command == "metrics":
        return _run_metrics(args)

    if args.command == "watcher":
        return _run_watcher(args)

    return _run_generate(args)


if __name__ == "__main__":
    sys.exit(main())
