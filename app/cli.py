import argparse
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from app.core.config import RepoConfig
from app.core.generator import generate
from app.core.metrics import MetricsStore
from app.core.post_setup import fetch_skills, run_git_init, run_precommit_install
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
        choices=sorted(_KEY_TO_FIELD),
        help="Preference key (use hyphens, e.g. author-name).",
    )
    cfg_set.add_argument("value", help="Value to store.")

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
        "--git-init", action="store_true", help="Run git init in the output directory."
    )
    gen.add_argument(
        "--install-precommit",
        action="store_true",
        help="Run pre-commit install in the output directory.",
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
        "--max-workers",
        type=int,
        default=1,
        help="Maximum number of concurrent worker sessions (default: 1).",
    )

    return parser


def _run_watcher(args: argparse.Namespace) -> int:
    from app.core.watcher import Watcher

    mode = args.worker_mode or os.environ.get("WORKER_MODE", "default")
    watcher = Watcher(worker_mode=mode, max_workers=args.max_workers)
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


def _run_config(args: argparse.Namespace) -> int:
    if args.config_cmd == "get":
        prefs = PrefsStore.load()
        for field, value in prefs.model_dump().items():
            key = field.replace("_", "-")
            print(f"{key}: {value}")
        return 0

    if args.config_cmd == "set":
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

    # config with no sub-subcommand
    print("Usage: scaffold config {get,set}", file=sys.stderr)
    return 1


def _run_generate(args: argparse.Namespace) -> int:
    try:
        config = RepoConfig(
            repo_name=args.repo_name,
            preset=args.preset,
            include_precommit=args.pre_commit,
            include_ci=args.ci,
            include_pr_template=args.pr_template,
            include_issue_templates=args.issue_templates,
            include_codeowners=args.codeowners,
            include_claude_files=args.claude_files,
            git_init=args.git_init,
            install_precommit=args.install_precommit,
        )
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        prefs = PrefsStore.load()
        written = generate(config, args.output, prefs)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for path in written:
        print(f"✓ {path}")

    preset = get_preset(config.preset)
    if preset.skills_source is not None and preset.skills_version is not None:
        skills_written = fetch_skills(
            args.output,
            skills_source=preset.skills_source,
            skills_version=preset.skills_version,
        )
        for path in skills_written:
            print(f"✓ {path}")

    try:
        if config.git_init:
            run_git_init(args.output)
            print("✓ git init")
        if config.install_precommit:
            run_precommit_install(args.output)
            print("✓ pre-commit install")
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

    if args.command == "config":
        return _run_config(args)

    if args.command == "metrics":
        return _run_metrics(args)

    if args.command == "watcher":
        return _run_watcher(args)

    return _run_generate(args)


if __name__ == "__main__":
    sys.exit(main())
