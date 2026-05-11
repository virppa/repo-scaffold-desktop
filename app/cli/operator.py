"""Watcher daemon controls + metrics CLI subcommands."""

import argparse
import os
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

from app.core.linear_client import LinearClient
from app.core.metrics import MetricsStore
from app.core.watcher.ticket_status import (
    ArtifactInfo,
    LogInfo,
    TicketStatus,
    _format_age,
    _format_size,
    fetch_ticket_status,
)
from app.core.watcher.watcher_daemon import (
    launch_detached,
    launch_in_new_terminal,
    load_env_file,
)


def _run_watcher(args: argparse.Namespace) -> int:
    # WOR-435: auto-load .env so agent-spawned / remote shells inherit
    # LINEAR_API_KEY without manual export. No-op if .env is absent.
    load_env_file()

    # WOR-435: --visible opens a new terminal window with the watcher
    # attached and exits. --detach spawns a fully background daemon and
    # exits. Both flags are mutually exclusive with each other and with
    # the normal foreground path.
    if args.detach and args.visible:
        print(
            "Error: --detach and --visible are mutually exclusive.",
            file=sys.stderr,
        )
        return 2
    if args.visible:
        return launch_in_new_terminal()
    if args.detach:
        pid = launch_detached()
        log_path = Path.cwd() / ".claude" / "watcher.log"
        print(
            f"Watcher daemon started (PID {pid}). Logs: {log_path}",
            file=sys.stderr,
        )
        return 0

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


def _run_watcher_forcestop(_args: argparse.Namespace) -> int:
    """Write the force-stop sentinel to terminate all active workers (WOR-352).

    The daemon polls .claude/watcher.forcestop each cycle: if present, it
    commits WIP for every active worker, terminates them, and pauses
    the dispatcher.
    """
    claude_dir = Path.cwd() / ".claude"
    pid_file = claude_dir / "watcher.pid"
    sentinel = claude_dir / "watcher.forcestop"
    if not pid_file.exists():
        print(
            "Error: watcher daemon not running (no .claude/watcher.pid). "
            "Force-stop is a no-op when no daemon is active.",
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
        f"Force-stop requested. Daemon (PID {pid_str}) will terminate "
        f"all active workers. Sentinel: {sentinel}",
        file=sys.stderr,
    )
    return 0


def _run_watcher_pause(_args: argparse.Namespace) -> int:
    """Write the pause sentinel to stop dispatch (WOR-352).

    The daemon polls .claude/watcher.pause each cycle: if present, it
    stops accepting new dispatches, promotions, and epic completions,
    but keeps reaping workers and health checks running.
    """
    claude_dir = Path.cwd() / ".claude"
    pid_file = claude_dir / "watcher.pid"
    sentinel = claude_dir / "watcher.pause"
    if not pid_file.exists():
        print(
            "Error: watcher daemon not running (no .claude/watcher.pid). "
            "Pause is a no-op when no daemon is active.",
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
        f"Pause requested. Daemon (PID {pid_str}) will stop dispatching. "
        f"Sentinel: {sentinel}",
        file=sys.stderr,
    )
    return 0


def _run_watcher_resume(_args: argparse.Namespace) -> int:
    """Remove the pause sentinel to resume dispatch (WOR-352)."""
    claude_dir = Path.cwd() / ".claude"
    pid_file = claude_dir / "watcher.pid"
    sentinel = claude_dir / "watcher.pause"
    if not pid_file.exists():
        print("Watcher is already running — pause is not active.", file=sys.stderr)
        return 0
    if not pid_file.exists():
        print(
            "Error: watcher daemon not running (no .claude/watcher.pid). "
            "Resume is a no-op when no daemon is active.",
            file=sys.stderr,
        )
        return 1
    try:
        sentinel.unlink()
        pid_str = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        pid_str = "unknown"
    print(
        f"Pause removed. Daemon (PID {pid_str}) will resume dispatching "
        f"on its next poll cycle.",
        file=sys.stderr,
    )
    return 0


def _run_watcher_kill(args: argparse.Namespace) -> int:
    """Write ticket IDs to the kill sentinel to target specific workers (WOR-352).

    Each line in the sentinel file is a ticket ID (e.g. WOR-123).
    The daemon terminates matching workers after committing their WIP.
    """
    claude_dir = Path.cwd() / ".claude"
    pid_file = claude_dir / "watcher.pid"
    sentinel = claude_dir / "watcher.kill"
    if not pid_file.exists():
        print(
            "Error: watcher daemon not running (no .claude/watcher.pid). "
            "Kill is a no-op when no daemon is active.",
            file=sys.stderr,
        )
        return 1
    claude_dir.mkdir(parents=True, exist_ok=True)
    # args.ticket_ids is populated by the parser as nargs="+".
    ticket_ids = getattr(args, "ticket_ids", None)
    if not ticket_ids:
        print(
            "Error: no ticket IDs specified.",
            file=sys.stderr,
        )
        return 1
    sentinel.write_text(
        "\n".join(t.strip().upper() for t in ticket_ids if t.strip()),
        encoding="utf-8",
    )
    try:
        pid_str = pid_file.read_text(encoding="utf-8").strip()
    except OSError:
        pid_str = "unknown"
    print(
        f"Kill requested for {len(ticket_ids)} ticket(s): "
        f"{', '.join(t.upper() for t in ticket_ids)}. "
        f"Daemon (PID {pid_str}) will process on next poll. Sentinel: {sentinel}",
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


def _check_ticket_status_api_key() -> str | None:
    """Return LINEAR_API_KEY or None (printing error to stderr)."""
    import os

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print("Error: LINEAR_API_KEY environment variable not set.", file=sys.stderr)
        return None
    return api_key


def _emit_worker_log_block(wl: LogInfo) -> None:
    """Print the worker-log section of the full status block."""
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


def _emit_artifacts_block(artifacts: ArtifactInfo) -> None:
    """Print the artifacts section of the full status block."""
    print(f"Artifacts ({artifacts.path}):")
    for name, info in artifacts.entries.items():
        print(f"  {name}    {info}")


def _emit_worktree_block(status: TicketStatus) -> None:
    """Print the worktree section of the full status block."""
    if status.worktree_exists is True:
        print(f"Worktree: {status.worktree_path}  exists")
    elif status.worktree_exists is False:
        print("Worktree: none")
    else:
        print("Worktree: ?")


def _emit_health_flags_block(flags: dict[str, object]) -> None:
    """Print the health-flags section (only when at least one flag is present)."""
    parts = []
    if "api_retries" in flags:
        parts.append(f"{flags['api_retries']} api_retry events")
    if "subagent_spawns" in flags:
        parts.append(f"{flags['subagent_spawns']} subagent spawns")
    if "no_result_artifact" in flags:
        parts.append("no result artifact yet")
    print(f"Health flags: {'; '.join(parts)}")


def _emit_ticket_status_full(status: TicketStatus) -> None:
    """Print the default human-readable status block."""
    print(f"Ticket: {status.ticket_id} — {status.title}")
    age_str = _format_age(status.state_age_seconds) if status.state_age_seconds else "?"
    print(f"State (Linear): {status.state} (age: {age_str})")

    if status.worker_log is not None:
        _emit_worker_log_block(status.worker_log)
    else:
        print("Worker process: no log file found")

    if status.artifacts is not None:
        _emit_artifacts_block(status.artifacts)
    else:
        print("Artifacts: none")

    _emit_worktree_block(status)

    if status.health_flags:
        _emit_health_flags_block(status.health_flags)


def _run_ticket_status_watch_loop(
    client: LinearClient, ticket_id: str, status: TicketStatus
) -> None:
    """Poll the ticket every 30s until it reaches a terminal state.

    Sonar S3516: the earlier int return had two identical `return 0`
    paths; the caller now wraps this in `return 0` itself.
    """
    terminal_states = {"Done", "MergedToEpic", "Cancelled", "Duplicate", "Blocked"}
    while status.state not in terminal_states:
        print(f"\n── polled {_format_age(status.state_age_seconds)} ──")
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            return
        status = fetch_ticket_status(client, ticket_id)
        print()
        _emit_ticket_status_full(status)
    print(f"\nTicket reached terminal state: {status.state}")


def _run_ticket_status(args: argparse.Namespace) -> int:
    """Show a structured snapshot of a Linear ticket's state."""
    ticket_id = args.ticket_id.upper()

    api_key = _check_ticket_status_api_key()
    if api_key is None:
        return 1

    client = LinearClient(api_key=api_key)
    status = fetch_ticket_status(client, ticket_id)

    if status.state == "Unknown":
        if "error fetching" in status.title:
            print(
                f"Error: {status.title}",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: {status.title} (fetching full status)",
                file=sys.stderr,
            )
        return 1

    if args.json:
        import json as json_mod

        print(json_mod.dumps(status.to_dict(), indent=2))
        return 0

    if args.brief:
        age_str = (
            _format_age(status.state_age_seconds) if status.state_age_seconds else "?"
        )
        print(f"{status.ticket_id} — {status.title} ({status.state}) — {age_str}")
        return 0

    _emit_ticket_status_full(status)

    if args.watch:
        _run_ticket_status_watch_loop(client, ticket_id, status)
        return 0

    return 0
