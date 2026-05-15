"""Argparse parser construction for `python -m app.cli`."""

import argparse
from pathlib import Path

from app.core.presets import _PRESETS
from app.core.user_prefs import UserPreferences

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
        choices=set(_KEY_TO_FIELD) | {"github-token"},
        help="Preference key (use hyphens, e.g. author-name).",
    )
    cfg_set.add_argument("value", nargs="?", default=None, help="Value to store.")

    cfg_del = cfg_sub.add_parser("delete", help="Delete a credential.")
    cfg_del.add_argument(
        "key",
        choices=set(_KEY_TO_FIELD) | {"github-token"},
        help="Credential key to delete.",
    )

    gen = sub.add_parser("generate", help="Generate scaffold files.")
    gen.add_argument(
        "--preset",
        required=False,
        choices=list(_PRESETS),
        help="Preset to use.",
    )
    gen.add_argument("--repo-name", required=False, help="Repository name.")
    gen.add_argument(
        "--output",
        required=False,
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
        "--no-rollback-on-failure",
        action="store_true",
        default=False,
        help=(
            "Skip rolling back a GitHub repo when configure or push fails. "
            "Without this flag, an orphaned repo is deleted on failure."
        ),
    )
    gen.add_argument(
        "--remote-url",
        default=None,
        help="Remote URL to push to (used with --git-push).",
    )
    gen.add_argument(
        "--interactive",
        action="store_true",
        help="Run an interactive wizard instead of using flags.",
    )
    gen.add_argument(
        "--manual-steps",
        action="store_true",
        default=False,
        help="Ask about each feature toggle in the wizard.",
    )
    gen.add_argument(
        "--save-defaults",
        action="store_true",
        default=False,
        help="Save wizard answers as default preferences after generation.",
    )
    gen.add_argument(
        "--prefill",
        action="store_true",
        default=False,
        help="Pre-fill wizard prompts from stored preferences.",
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

    waste = sub.add_parser(
        "waste-score",
        help="Compute and print the waste score for a worker session log.",
    )
    waste.add_argument(
        "ticket_id",
        help="Ticket ID (e.g. WOR-277).",
    )

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
        "--max-concurrent-checks",
        type=int,
        default=None,
        help=(
            "Maximum number of worker finalize sweeps "
            "(ruff/mypy/pytest/lint-imports) running in parallel (WOR-451). "
            "Default: max(max_local_workers // 2, 2). Lower this on memory-"
            "constrained boxes; set to 1 to revert to fully serial finalize."
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
    watcher.add_argument(
        "--kv-budget",
        type=int,
        default=None,
        help=(
            "KV-token budget for local-dispatch admission control (WOR-502). "
            "Each ticket reserves tokens proportional to its effort level; "
            "the watcher admits a worker only while the sum of in-flight "
            "reservations plus the candidate stays within budget. "
            "Default: None (off — admit based on --max-local-workers only)."
        ),
    )
    # WOR-435: programmatic launch flags.
    watcher.add_argument(
        "--detach",
        action="store_true",
        default=False,
        help=(
            "Run the watcher as a detached background daemon. Parent exits "
            "immediately; child writes .claude/watcher.pid and streams logs "
            "to .claude/watcher.log. Useful for agent-driven launches and "
            "remote/mobile dispatch flows."
        ),
    )
    watcher.add_argument(
        "--visible",
        action="store_true",
        default=False,
        help=(
            "Windows only: open a new visible cmd.exe window with the "
            "watcher running attached. .env is auto-loaded by the child. "
            "Falls back with a clear error on non-Windows platforms."
        ),
    )

    # WOR-333: graceful drain/stop signal.
    sub.add_parser(
        "watcher-softstop",
        help=(
            "Signal the watcher daemon to enter drain mode: stop accepting "
            "new dispatches, finish in-flight workers, then exit cleanly. "
            "Writes a sentinel file the daemon polls each cycle."
        ),
    )

    # WOR-352: daemon-control gestures.
    sub.add_parser(
        "watcher-forcestop",
        help=(
            "Signal the watcher daemon to terminate all active workers. "
            "Commits WIP for each worker, then terminates. Pauses dispatcher."
        ),
    )
    sub.add_parser(
        "watcher-pause",
        help=(
            "Signal the watcher daemon to pause dispatch. Stops accepting "
            "new dispatches, promotions, and epic completions. Keeps "
            "reaping and health checks running."
        ),
    )
    sub.add_parser(
        "watcher-resume",
        help=(
            "Remove the pause sentinel so the watcher daemon resumes "
            "dispatching tickets."
        ),
    )
    kill_p = sub.add_parser(
        "watcher-kill",
        help=(
            "Terminate one or more specific active workers by ticket ID. "
            "Each line is a ticket ID (e.g. WOR-123). Silently skips "
            "IDs not found among active workers."
        ),
    )
    kill_p.add_argument(
        "ticket_ids",
        nargs="+",
        help="One or more ticket IDs to kill (e.g. WOR-123 WOR-456).",
    )

    # WOR-337: ticket-status subcommand — structured ticket snapshot.
    ts = sub.add_parser(
        "ticket-status",
        help="Show a structured snapshot of a ticket's state.",
    )
    ts.add_argument("ticket_id", help="Linear ticket ID, e.g. WOR-123")
    ts.add_argument(
        "--watch",
        action="store_true",
        default=False,
        help="Re-poll every 30s until the ticket reaches a terminal state.",
    )
    ts.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON output.",
    )
    ts.add_argument(
        "--brief",
        action="store_true",
        default=False,
        help="Emit a single-line summary suitable for status bars.",
    )

    return parser
