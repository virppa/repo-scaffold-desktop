"""Live TUI display for the watcher orchestrator.

Rendered with ``rich.live.Live`` when ``--tui`` is active and stderr is a
terminal.  Falls back to line-based logging (via the existing ``ColorFormatter``)
when stderr is piped or redirected.

Usage
-----
``WatcherDisplay`` follows a push model: the watcher poll loop calls
``display.update_state(...)`` once per cycle.  The rich ``Live`` widget
handles its own refresh loop — no background threading inside the display.
"""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.table import Table

from app.core.metrics import CostRollup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes pushed to the display
# ---------------------------------------------------------------------------


@dataclass
class TrackedPR:
    """A single GitHub PR under auto-merge tracking."""

    number: int
    base: str
    last_poll: float = 0.0
    last_status: str = "PENDING"
    dropped_at: float = 0.0  # when to drop from the list after MERGED


@dataclass
class WorkerState:
    """Runtime state of a single worker session."""

    ticket_id: str
    mode: str  # "local" | "cloud"
    status: str  # e.g. "running", "success", "failed"
    elapsed_s: float = 0.0
    cloud_cost: float = 0.0  # only when mode == "cloud"
    local_saved: float = 0.0  # only when mode == "local"


@dataclass
class TUIState:
    """Complete snapshot pushed by the watcher every poll cycle."""

    workers: list[WorkerState] = field(default_factory=list)
    cost_rollups: dict[str, CostRollup] = field(
        default_factory=lambda: {
            "today": CostRollup(),
            "week": CostRollup(),
            "all": CostRollup(),
        }
    )
    tracked_prs: list[TrackedPR] = field(default_factory=list)


# ---------------------------------------------------------------------------
# TTY gating
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    """Return True when stderr looks like an interactive terminal."""
    return sys.stderr.isatty()


# ---------------------------------------------------------------------------
# WatcherDisplay — the public API
# ---------------------------------------------------------------------------


class WatcherDisplay:
    """Live TUI display (rich) or line-based logging fallback."""

    def __init__(self, *, console: Console | None = None) -> None:
        self._state = TUIState()
        self._console = console or Console()
        self._live: Live | None = None
        self._rollup_cache: dict[str, tuple[float, CostRollup]] = {}

    # ------------------------------------------------------------------
    # State update — single entry point from watcher poll loop
    # ------------------------------------------------------------------

    def update_state(self, state: TUIState) -> None:
        """Push a new snapshot.  Triggers a render cycle."""
        self._state = state
        if _is_tty():
            self._render_live()
        else:
            self._render_line(state)

    # ------------------------------------------------------------------
    # Live (rich) rendering
    # ------------------------------------------------------------------

    def _render_live(self) -> None:
        if not _is_tty():
            self._render_line(self._state)
            return
        layout = self._build_layout(self._state)
        if self._live is None:
            self._live = Live(
                layout,
                console=self._console,
                screen=True,
                auto_refresh=False,
            )
            self._live.start(refresh=True)
            return
        self._live.update(layout, refresh=True)

    def _build_layout(self, state: TUIState) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="top", size=3),
            Layout(name="middle"),
            Layout(name="bottom", size=1),
        )
        layout["top"].split_row(self._cost_table(state), self._rollup_table(state))
        layout["middle"].split_row(self._worker_table(state), self._pr_table(state))
        layout["bottom"].update("Ctrl-C to exit  |  [dim]refresh every ~30s[/dim]")
        return layout

    # ------------------------------------------------------------------
    # Sub-widgets
    # ------------------------------------------------------------------

    @staticmethod
    def _format_cost(value: float) -> str:
        if value < 0.01:
            return f"${value:.4f}"
        return f"${value:.2f}"

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        mins = int(seconds) // 60
        secs = int(seconds) % 60
        return f"{mins}m{secs:02d}s"

    def _cost_table(self, state: TUIState) -> Table:
        table = Table(title="Cost Economics", show_header=True, expand=True)
        table.add_column("Period", style="cyan")
        table.add_column("Cloud Spent", justify="right")
        table.add_column("Local Saved", justify="right")
        table.add_column("Cloud #", justify="right")
        table.add_column("Local #", justify="right")
        for period in ("today", "week", "all"):
            cr = state.cost_rollups.get(period, CostRollup())
            if period in state.cost_rollups:
                table.add_row(
                    period.capitalize(),
                    self._format_cost(cr.cloud_spent),
                    self._format_cost(cr.local_saved),
                    str(cr.cloud_ticket_count),
                    str(cr.local_ticket_count),
                )
        return table

    @staticmethod
    def _rollup_table(state: TUIState) -> Table:
        table = Table(title="Session Totals", show_header=True, expand=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")
        total_cloud = sum(w.cloud_cost for w in state.workers if w.mode == "cloud")
        total_local = sum(w.local_saved for w in state.workers if w.mode == "local")
        table.add_row("Session Cloud Spent", "$" + f"{total_cloud:.4f}")
        table.add_row("Session Local Saved", "$" + f"{total_local:.4f}")
        table.add_row("Active Workers", str(len(state.workers)))
        for pr in state.tracked_prs:
            if pr.last_status == "MERGED":
                table.add_row(f"PR #{pr.number}", "MERGED")
        return table

    @staticmethod
    def _worker_table(state: TUIState) -> Table:
        table = Table(title="Workers", show_header=True, expand=True)
        table.add_column("Ticket", style="cyan")
        table.add_column("Mode")
        table.add_column("Status")
        table.add_column("Elapsed", justify="right")
        table.add_column("Cost", justify="right")
        for w in state.workers:
            cost_str = ""
            if w.mode == "cloud":
                cost_str = f"${w.cloud_cost:.4f}"
            elif w.mode == "local":
                cost_str = f"${w.local_saved:.4f} saved"
            table.add_row(
                w.ticket_id,
                w.mode,
                w.status,
                WatcherDisplay._format_elapsed(w.elapsed_s),
                cost_str,
            )
        if not state.workers:
            table.add_row("—", "—", "No active workers", "—", "—")
        return table

    @staticmethod
    def _pr_table(state: TUIState) -> Table:
        table = Table(title="PR Auto-Merge Tracker", show_header=True, expand=True)
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Base Branch")
        table.add_column("Status")
        table.add_column("Last Poll", justify="right")
        active: list[TrackedPR] = []
        for pr in state.tracked_prs:
            if pr.dropped_at > 0 and time.time() >= pr.dropped_at:
                continue
            active.append(pr)
        for pr in active:
            status_style = ""
            if pr.last_status in ("CONFLICTING", "BLOCKED"):
                status_style = "red"
            elif pr.last_status == "MERGED":
                status_style = "green"
            table.add_row(
                str(pr.number),
                pr.base,
                f"[{status_style}]{pr.last_status}[/{status_style}]"
                if status_style
                else pr.last_status,
                f"{time.time() - pr.last_poll:.0f}s",
            )
        if not active:
            table.add_row("—", "—", "No tracked PRs", "—")
        return table

    # ------------------------------------------------------------------
    # Line-based fallback
    # ------------------------------------------------------------------

    def _render_line(self, state: TUIState) -> None:
        lines: list[str] = []
        # Workers
        for w in state.workers:
            cost_info = ""
            if w.mode == "cloud":
                cost_info = f"  cost=${w.cloud_cost:.4f}"
            elif w.mode == "local":
                cost_info = f"  saved=${w.local_saved:.4f}"
            lines.append(
                f"[WOR-{w.ticket_id}] {w.mode} {w.status} "
                f"{self._format_elapsed(w.elapsed_s)}{cost_info}"
            )
        # PR tracking
        for pr in state.tracked_prs:
            if pr.dropped_at > 0 and time.time() >= pr.dropped_at:
                continue
            lines.append(f"[PR #{pr.number}] base={pr.base} status={pr.last_status}")
        # Cost rollup summary
        for period in ("today", "week", "all"):
            cr = state.cost_rollups.get(period, CostRollup())
            if period in state.cost_rollups:
                lines.append(
                    f"[COST {period.capitalize()}] "
                    f"cloud_spent={cr.cloud_spent:.4f} local_saved={cr.local_saved:.4f}"
                )
        for msg in lines:
            logger.info("TUI: %s", msg)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop the live display.

        Best-effort: rich.live.Live.stop() can raise OSError on broken/closed
        terminal handles during shutdown. Swallow that case explicitly so the
        watcher's cleanup path completes; let unexpected exceptions bubble.
        """
        if self._live is not None:
            try:
                self._live.stop()
            except OSError as exc:
                logger.debug("Live.stop() failed during shutdown: %s", exc)
            self._live = None

    def update_pr(
        self,
        number: int,
        base: str,
        last_status: str,
        *,
        poll_time: float | None = None,
    ) -> None:
        """Update or register a PR status entry.

        On first call registers the PR; on subsequent calls updates
        ``last_status`` and ``last_poll``.
        """
        for pr in self._state.tracked_prs:
            if pr.number == number:
                pr.last_status = last_status
                pr.last_poll = poll_time or time.time()
                return
        self._state.tracked_prs.append(
            TrackedPR(
                number=number,
                base=base,
                last_poll=poll_time or time.time(),
                last_status=last_status,
            )
        )

    def add_worker(self, worker: WorkerState) -> None:
        """Register a worker for tracking."""
        self._state.workers.append(worker)

    def remove_worker(self, ticket_id: str) -> None:
        """Remove a worker by ticket ID."""
        self._state.workers = [
            w for w in self._state.workers if w.ticket_id != ticket_id
        ]

    # ------------------------------------------------------------------
    # gh subprocess PR polling
    # ------------------------------------------------------------------

    @staticmethod
    def poll_pr_status(number: int) -> tuple[str, str]:
        """Run ``gh pr view`` and return (state, mergeStateStatus).

        On any error (network, gone PR, auth expiry) return ``('?','?')``
        to silently demote the PR to unknown status.
        """
        try:
            result = subprocess.run(  # nosec B603 B607
                [
                    "gh",
                    "pr",
                    "view",
                    str(number),
                    "--json",
                    "mergeable,mergeStateStatus,state",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                return ("?", "?")
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                return ("?", "?")
            state = data.get("state", "?") if isinstance(data, dict) else "?"
            merge_status = (
                data.get("mergeStateStatus", "?") if isinstance(data, dict) else "?"
            )
            return (state, merge_status)
        except Exception:
            return ("?", "?")
