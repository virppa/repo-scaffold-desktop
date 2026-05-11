"""Top-level CLI entry: load_dotenv, dispatch, main()."""

import argparse
import sys

from dotenv import load_dotenv

from app.cli.config import _run_config
from app.cli.generate import _run_generate
from app.cli.operator import (
    _run_metrics,
    _run_ticket_status,
    _run_waste_score,
    _run_watcher,
    _run_watcher_softstop,
)
from app.cli.parser import _build_parser


def _dispatch_command(args: argparse.Namespace) -> int:
    """Route parsed args to the appropriate command handler.

    Separated from ``main()`` to keep its cognitive complexity under 15.
    """
    if args.command == "config":
        return _run_config(args)
    if args.command == "metrics":
        return _run_metrics(args)
    if args.command == "watcher":
        return _run_watcher(args)
    if args.command == "watcher-softstop":
        return _run_watcher_softstop(args)
    if args.command == "waste-score":
        return _run_waste_score(args)
    if args.command == "ticket-status":
        return _run_ticket_status(args)
    if args.command == "generate":
        return _run_generate(args)
    return 1


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

    return _dispatch_command(args)
