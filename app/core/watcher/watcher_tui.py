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
from rich.logging import RichHandler
from rich.table import Table

from app.core.metrics import CostRollup, RoutingDistribution

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
class QueueState:
    """Queue summary for the TUI queue panel."""

    ready: int = 0
    waiting: int = 0
    in_progress: int = 0
    blocked: int = 0


@dataclass
class WorkerState:
    """Runtime state of a single worker session."""

    ticket_id: str
    mode: str  # "local" | "cloud"
    status: str  # e.g. "running", "success", "failed"
    elapsed_s: float = 0.0
    cloud_cost: float = 0.0  # only when mode == "cloud"
    local_saved: float = 0.0  # only when mode == "local"
    last_action: str = ""


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
    vllm_metrics: dict[str, float] | None = None
    queue_state: QueueState = field(default_factory=QueueState)
    routing_distribution: RoutingDistribution = field(
        default_factory=RoutingDistribution
    )


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
        self._logger_handler: logging.Handler | None = None
        self._original_handlers: list[logging.Handler] | None = None

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
            self._install_log_handler()
            return
        self._live.update(layout, refresh=True)

    def _build_layout(self, state: TUIState) -> Layout:
        layout = Layout(name="root")
        layout.split_column(
            Layout(name="top", size=3),
            Layout(name="middle"),
            Layout(name="bottom"),
        )
        layout["top"].split_row(
            self._cost_table(state),
            self._routing_table(state),
            self._rollup_table(state),
        )
        # Workers claims the grow weight (middle = one panel only).
        layout["middle"].update(
            Layout(self._worker_table(state), name="workers", ratio=3),
        )
        # Low-density panels render at content height — no padding-to-fill.
        bottom = Layout()
        bottom.split_column(
            Layout(self._vllm_table(state), name="vllm", size=0),
            Layout(self._queue_table(state), name="queue", size=0),
            Layout(self._pr_table(state), name="pr", size=0),
        )
        layout["bottom"].update(bottom)
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
        all_empty = not any(
            (isinstance(v, CostRollup) and (v.cloud_spent != 0 or v.local_saved != 0))
            for v in (
                state.cost_rollups if isinstance(state.cost_rollups, dict) else {}
            ).values()
        )
        if all_empty:
            table.add_row("—", "—", "No data yet", "—", "—")
            return table
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
        if not state.workers:
            table.add_row("—", "—", "No data yet", "—")
            return table
        total_cloud = sum(w.cloud_cost for w in state.workers if w.mode == "cloud")
        total_local = sum(w.local_saved for w in state.workers if w.mode == "local")
        table.add_row("Session Cloud Spent", "$" + f"{total_cloud:.4f}")
        table.add_row("Session Local Saved", "$" + f"{total_local:.4f}")
        today_rollup = state.cost_rollups.get("today", CostRollup())
        if isinstance(today_rollup, CostRollup):
            table.add_row(
                "Tickets Dispatched Today",
                str(today_rollup.cloud_ticket_count + today_rollup.local_ticket_count),
            )
            table.add_row(
                "Total Cost Saved Estimate",
                "$" + f"{today_rollup.local_saved:.4f}",
            )
        table.add_row("Active Workers", str(len(state.workers)))
        for pr in state.tracked_prs:
            if pr.last_status == "MERGED":
                table.add_row(f"PR #{pr.number}", "MERGED")
        return table

    def _routing_table(self, state: TUIState) -> Table:
        rd = state.routing_distribution
        table = Table(title="Routing", show_header=True, expand=True)
        table.add_column("Route", style="cyan")
        table.add_column("Count", justify="right")
        table.add_column("Saved", justify="right")
        has_data = (
            rd.local_preferred_count
            or rd.cloud_preferred_count
            or rd.cloud_only_count
            or rd.total_local_saved
            or rd.total_savings
        )
        if not has_data:
            table.add_row("—", "—", "No data yet", "—")
            return table
        # local_preferred — tickets that cost-economics consider local-first
        table.add_row(
            "Local Pref",
            str(rd.local_preferred_count),
            self._format_cost(rd.total_local_saved),
        )
        # cloud_preferred breakdown
        cloud_pref_total = rd.cloud_preferred_local_ran + rd.cloud_preferred_cloud_ran
        table.add_row(
            "Cloud Pref",
            str(cloud_pref_total),
            self._format_cost(rd.total_cloud_cost),
        )
        if cloud_pref_total:
            local_pct = (rd.cloud_preferred_local_ran / cloud_pref_total) * 100
            table.add_row(
                "  Ran Local",
                str(rd.cloud_preferred_local_ran),
                f"{local_pct:.0f}%",
            )
        table.add_row(
            "Cloud Only",
            str(rd.cloud_only_count),
            "—",
        )
        table.add_row(
            "Total Savings",
            "",
            self._format_cost(rd.total_savings),
            "",
        )
        return table

    @staticmethod
    def _worker_table(state: TUIState) -> Table:
        table = Table(title="Workers", show_header=True, expand=True)
        table.add_column("Ticket", style="cyan")
        table.add_column("Mode")
        table.add_column("Status")
        table.add_column("Elapsed", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Last Action", style="dim")
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
                w.last_action,
            )
        if not state.workers:
            table.add_row("—", "—", "No active workers", "—", "—", "—")
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

    @staticmethod
    def _vllm_table(state: TUIState) -> Table:
        table = Table(title="vLLM", show_header=True, expand=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")
        metrics = state.vllm_metrics
        if metrics is None:
            table.add_row("—", "No vLLM running")
            return table
        _fill_vllm_rows(table, metrics)
        return table

    @staticmethod
    def _queue_table(state: TUIState) -> Table:
        qs = state.queue_state
        table = Table(title="Queue", show_header=True, expand=True)
        table.add_column("State", style="cyan")
        table.add_column("Count", justify="right")
        table.add_row("ReadyForLocal", str(qs.ready))
        table.add_row("WaitingForDeps", str(qs.waiting))
        table.add_row("InProgressLocal", str(qs.in_progress))
        table.add_row("Blocked", str(qs.blocked))
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
            last_act = f" action={w.last_action}" if w.last_action else ""
            lines.append(
                f"[WOR-{w.ticket_id}] {w.mode} {w.status} "
                f"{self._format_elapsed(w.elapsed_s)}{cost_info}{last_act}"
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
        # Routing distribution summary
        rd = state.routing_distribution
        if rd.local_preferred_count or rd.cloud_preferred_count or rd.cloud_only_count:
            lines.append(
                f"[ROUTING] local_pref={rd.local_preferred_count} "
                f"cloud_pref={rd.cloud_preferred_count} "
                f"cloud_only={rd.cloud_only_count} "
                f"savings={rd.total_savings:.4f}"
            )
        for msg in lines:
            logger.info("TUI: %s", msg)

    # ------------------------------------------------------------------
    # Live-aware logging handler lifecycle (WOR-448)
    # ------------------------------------------------------------------

    def _install_log_handler(self) -> None:
        """Replace root logger handlers with a RichHandler bound to ``self._console``.

        The ``RichHandler`` writes log output to the same console that the
        ``rich.Live`` widget renders to, keeping log messages visible to the
        operator while preventing them from escaping to the raw stderr stream
        and corrupting the Live frame.

        The original root logger handlers are captured so they can be restored
        exactly on :meth:`stop`.
        """
        root = logging.getLogger()
        self._original_handlers = list(root.handlers)
        self._logger_handler = RichHandler(console=self._console)
        root.handlers = [self._logger_handler]

    def _remove_log_handler(self) -> None:
        """Restore the original root logger handlers.

        Called on :meth:`stop` to revert the handler swap so that non-TTY
        logging continues to work as before.
        """
        if self._logger_handler is not None:
            root = logging.getLogger()
            if self._original_handlers is not None:
                root.handlers = self._original_handlers
            else:
                root.handlers = [self._logger_handler]
            self._logger_handler = None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Stop the live display and restore the original root logger handlers.

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
        self._remove_log_handler()

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


# ---------------------------------------------------------------------------
# Module-level vLLM-table helpers (extracted from _vllm_table for CC, S3776)
# ---------------------------------------------------------------------------


def _fill_vllm_rows(table: Table, metrics: dict[str, float]) -> None:
    """Append the metric rows to the vLLM table.

    Each helper formats one row; this keeps _vllm_table's branching cheap.
    """
    gen = metrics.get("vllm:generation_tokens_total") or 0
    prompt = metrics.get("vllm:prompt_tokens_total") or 0
    total_gen = gen + prompt
    preempt = metrics.get("vllm:num_preemptions_total") or 0

    table.add_row("Tokens", f"{total_gen:,.0f}" if total_gen > 0 else "—")
    table.add_row("Queue Depth", f"{int(preempt)}" if preempt else "—")
    table.add_row(
        "Cache Hit Ratio",
        _format_hit_ratio(metrics.get("vllm:prefix_cache_hit_ratio")),
    )
    table.add_row(
        "TTFT Mean",
        _format_ttft_mean(metrics.get("vllm:ttft_mean_seconds")),
    )


def _format_hit_ratio(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "—"


def _format_ttft_mean(value: float | None) -> str:
    if value is None or value <= 0:
        return "—"
    return f"{value:.3f}s"
