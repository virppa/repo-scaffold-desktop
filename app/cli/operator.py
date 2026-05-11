"""Watcher daemon controls + metrics CLI subcommands."""

import argparse
import os
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

from app.core.metrics import MetricsStore


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


def _run_watcher_softstop(_args: argparse.Namespace) -> int:
    """Write the soft-stop sentinel so the daemon enters drain mode (WOR-333).

    Daemon polls .claude/watcher.softstop each cycle: if present, it stops
    accepting new dispatches, finishes in-flight workers, then exits cleanly.
    """
    claude_dir = Path.cwd() / ".claude"
    pid_file = claude_dir / "watcher.pid"
    sentinel = claude_dir / "watcher.softstop"
    if not pid_file.exists():
        print(
            "Error: watcher daemon not running (no .claude/watcher.pid). "
            "Soft-stop is a no-op when no daemon is active.",
            file=sys.stderr,
        )
        return 1
    claude_dir.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    try:
        pid_str = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        pid_str = "unknown"
    print(
        f"Soft-stop requested. Daemon (PID {pid_str}) will exit after "
        f"current workers finish. Sentinel: {sentinel}",
        file=sys.stderr,
    )
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


def _run_waste_score(args: argparse.Namespace) -> int:
    from app.core.watcher.worker_waste import compute_waste_score

    # The worker log lives in .claude/ within the repo root.
    # We search for it relative to the current working directory.
    ticket_id = args.ticket_id.lower()
    log_path = Path(".claude") / f"worker_{ticket_id}.log"

    if not log_path.exists():
        print(
            f"Error: worker log not found at {log_path}",
            file=sys.stderr,
        )
        return 1

    report = compute_waste_score(log_path)
    print(f"Ticket: {args.ticket_id}")
    print(f"Waste score: {report.score}/100")
    print("Breakdown:")
    for key, value in sorted(
        report.breakdown.items(), key=lambda x: x[1], reverse=True
    ):
        if value > 0:
            print(f"  {key}: {value}")
    return 0


def _run_ticket_status(args: argparse.Namespace) -> int:
    """Show a structured snapshot of a Linear ticket's state."""
    import os

    ticket_id = args.ticket_id.upper()

    # Require LINEAR_API_KEY for the Linear client.
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print(
            "Error: LINEAR_API_KEY environment variable not set.",
            file=sys.stderr,
        )
        return 1

    from app.core.linear_client import LinearClient

    client = LinearClient(api_key=api_key)

    from app.core.watcher.ticket_status import (
        _format_age,
        _format_size,
        fetch_ticket_status,
    )

    status = fetch_ticket_status(client, ticket_id)

    if args.json:
        import json as json_mod

        print(json_mod.dumps(status.to_dict(), indent=2))
        return 0

    if args.brief:
        # One-line: WOR-NNN — Title (State) — age
        ts = status.ticket_id
        title = status.title
        st = status.state
        age_str = (
            _format_age(status.state_age_seconds) if status.state_age_seconds else "?"
        )
        print(f"{ts} — {title} ({st}) — {age_str}")
        return 0

    # Default: human-readable structured output.
    print(f"Ticket: {status.ticket_id} — {status.title}")
    age_str = _format_age(status.state_age_seconds) if status.state_age_seconds else "?"
    print(f"State (Linear): {status.state} (age: {age_str})")

    # Worker process info
    if status.worker_log is not None:
        wl = status.worker_log
        size_str = _format_size(wl.size_bytes)
        last_ago = (
            _format_age(wl.last_activity_ago_seconds)
            if wl.last_activity_ago_seconds
            else "?"
        )
        print("Worker process:")
        print(f"  Log: {size_str}, last activity {last_ago}")
        if wl.last_tool_calls:
            print(f"  Recent actions (last {min(3, len(wl.last_tool_calls))}):")
            for tc in wl.last_tool_calls[:3]:
                print(f"    {tc.name} {tc.display}")
        else:
            print("  Recent actions: ?")
    else:
        print("Worker process: no log file found")

    # Artifacts
    if status.artifacts is not None:
        print(f"Artifacts ({status.artifacts.path}):")
        for name, info in status.artifacts.entries.items():
            print(f"  {name}    {info}")
    else:
        print("Artifacts: none")

    # Worktree
    if status.worktree_exists is True:
        print(f"Worktree: {status.worktree_path}  exists")
    elif status.worktree_exists is False:
        print("Worktree: none")
    else:
        print("Worktree: ?")

    # Health flags
    if status.health_flags:
        parts = []
        if "api_retries" in status.health_flags:
            retries = status.health_flags["api_retries"]
            parts.append(f"{retries} api_retry events")
        if "subagent_spawns" in status.health_flags:
            spawns = status.health_flags["subagent_spawns"]
            parts.append(f"{spawns} subagent spawns")
        if "no_result_artifact" in status.health_flags:
            parts.append("no result artifact yet")
        print(f"Health flags: {'; '.join(parts)}")

    # Watch mode: keep polling until terminal state or Ctrl+C
    if args.watch:
        terminal_states = {
            "Done",
            "MergedToEpic",
            "Cancelled",
            "Duplicate",
            "Blocked",
        }
        while status.state not in terminal_states:
            print(f"\n── polled {_format_age(status.state_age_seconds)} ──")
            try:
                time.sleep(30)
            except KeyboardInterrupt:
                return 0
            status = fetch_ticket_status(client, ticket_id)
            # Re-render output (strip ANSI from above)
            print(f"\nTicket: {status.ticket_id} — {status.title}")
            age_str = (
                _format_age(status.state_age_seconds)
                if status.state_age_seconds
                else "?"
            )
            print(f"State (Linear): {status.state} (age: {age_str})")
            if status.worker_log is not None:
                wl = status.worker_log
                size_str = _format_size(wl.size_bytes)
                last_ago = (
                    _format_age(wl.last_activity_ago_seconds)
                    if wl.last_activity_ago_seconds
                    else "?"
                )
                print("Worker process:")
                print(f"  Log: {size_str}, last activity {last_ago}")
                if wl.last_tool_calls:
                    print(f"  Recent actions (last {min(3, len(wl.last_tool_calls))}):")
                    for tc in wl.last_tool_calls[:3]:
                        print(f"    {tc.name} {tc.display}")
                else:
                    print("  Recent actions: ?")
            else:
                print("Worker process: no log file found")
            if status.artifacts is not None:
                print(f"Artifacts ({status.artifacts.path}):")
                for name, info in status.artifacts.entries.items():
                    print(f"  {name}    {info}")
            else:
                print("Artifacts: none")
            if status.worktree_exists is True:
                print(f"Worktree: {status.worktree_path}  exists")
            elif status.worktree_exists is False:
                print("Worktree: none")
            else:
                print("Worktree: ?")
            if status.health_flags:
                parts = []
                if "api_retries" in status.health_flags:
                    retries = status.health_flags["api_retries"]
                    parts.append(f"{retries} api_retry events")
                if "subagent_spawns" in status.health_flags:
                    spawns = status.health_flags["subagent_spawns"]
                    parts.append(f"{spawns} subagent spawns")
                if "no_result_artifact" in status.health_flags:
                    parts.append("no result artifact yet")
                print(f"Health flags: {'; '.join(parts)}")
        print(f"\nTicket reached terminal state: {status.state}")
        return 0

    return 0
