import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from app.core.config import RepoConfig
from app.core.generator import generate
from app.core.presets import _PRESETS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scaffold",
        description="Generate a repository scaffold from a preset.",
    )
    sub = parser.add_subparsers(dest="command")

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

    return parser


def main(argv: list[str] | None = None) -> int:
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
        )
    except ValidationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        written = generate(config, args.output)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    for path in written:
        print(f"✓ {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
