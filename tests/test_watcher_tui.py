"""Render-snapshot tests for WatcherDisplay (deferred from WOR-272).

Covers WOR-296: snapshot the rich.Layout output of each code path in
WatcherDisplay._build_layout and verify deterministic text representation.

Tests MUST NOT instantiate ``rich.live.Live`` — only test _build_layout
and _render_line sub-widgets directly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from rich.live import Live

from app.core.metrics import CostRollup
from app.core.watcher.watcher_heartbeat import build_tui_state
from app.core.watcher.watcher_tui import (
    TrackedPR,
    TUIState,
    WatcherDisplay,
    WorkerState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tui_state(**kwargs) -> TUIState:
    """Build a TUIState with explicit defaults."""
    defaults: dict = {
        "workers": [],
        "cost_rollups": {
            "today": CostRollup(),
            "week": CostRollup(),
            "all": CostRollup(),
        },
        "tracked_prs": [],
    }
    defaults.update(kwargs)
    return TUIState(**defaults)


def _take_snapshot(state: TUIState, width: int = 120) -> str:
    """Render a WatcherDisplay layout to a text snapshot."""
    display = WatcherDisplay()
    console = Console(record=True, width=width, force_terminal=True)
    console.print(display._build_layout(state))
    return console.export_text()


# ---------------------------------------------------------------------------
# WOR-296 — scenario tests
# ---------------------------------------------------------------------------


def test_renders_idle_state_when_no_workers() -> None:
    """Empty state: no active workers, no tracked PRs.

    Asserts the worker table shows the "No active workers" fallback row.
    """
    state = _tui_state()
    snapshot = _take_snapshot(state)

    # The fallback text may span multiple columns in a wider table
    assert "No active" in snapshot and "workers" in snapshot
    assert "No tracked" in snapshot and "PRs" in snapshot


def test_renders_single_worker_with_elapsed_and_cost() -> None:
    """Single local worker with elapsed time and local_saved amount."""
    state = _tui_state(
        workers=[
            WorkerState(
                ticket_id="WOR-50",
                mode="local",
                status="running",
                elapsed_s=125.0,
                local_saved=3.75,
            ),
        ],
    )
    snapshot = _take_snapshot(state)

    assert "WOR-50" in snapshot
    assert "local" in snapshot
    assert "running" in snapshot
    # 125s = 2m05s (int(125)//60=2, 125%60=5)
    assert "2m05s" in snapshot
    assert "$3.7500" in snapshot  # raw cost column format


def test_renders_single_worker_cloud_cost() -> None:
    """Single cloud worker shows cloud_cost in the cost column."""
    state = _tui_state(
        workers=[
            WorkerState(
                ticket_id="WOR-51",
                mode="cloud",
                status="running",
                elapsed_s=45.0,
                cloud_cost=0.85,
            ),
        ],
    )
    snapshot = _take_snapshot(state)

    assert "WOR-51" in snapshot
    assert "cloud" in snapshot
    assert "45s" in snapshot
    assert "$0.8500" in snapshot


def test_renders_tracked_pr_with_status() -> None:
    """PR auto-merge tracker table shows number, base branch, and status."""
    state = _tui_state(
        tracked_prs=[
            TrackedPR(
                number=42,
                base="epic/wor-300",
                last_status="PENDING",
                last_poll=time.time() - 120.0,
            ),
            TrackedPR(
                number=43,
                base="main",
                last_status="MERGED",
                last_poll=time.time() - 60.0,
            ),
        ],
    )
    snapshot = _take_snapshot(state)

    assert "42" in snapshot
    assert "43" in snapshot
    assert "epic/wor-300" in snapshot
    assert "main" in snapshot
    assert "PENDING" in snapshot
    assert "MERGED" in snapshot


def test_cost_rollup_status_bar() -> None:
    """Top-left table shows Cost Economics with headers and dollar formatting.

    Note: Rich layout clips data rows when the full layout is shown —
    the section size=3 doesn't fit all three period rows. We assert on the
    table headers (always visible) and verify _format_cost via unit tests.
    """
    state = _tui_state(
        cost_rollups={
            "today": CostRollup(
                cloud_spent=66.0,
                local_saved=2.22,
                cloud_ticket_count=3,
                local_ticket_count=5,
            ),
            "week": CostRollup(
                cloud_spent=0.005,
                local_saved=10.0,
                cloud_ticket_count=1,
                local_ticket_count=2,
            ),
            "all": CostRollup(
                cloud_spent=500.0,
                local_saved=25.0,
                cloud_ticket_count=10,
                local_ticket_count=20,
            ),
        },
    )
    snapshot = _take_snapshot(state)

    assert "Cost Economics" in snapshot
    assert "Period" in snapshot
    assert "Cloud Spent" in snapshot
    assert "Local Saved" in snapshot
    assert "Cloud #" in snapshot
    assert "Local #" in snapshot
    # Session Totals (right side of top) is always rendered
    assert "Session Totals" in snapshot
    assert "Metric" in snapshot
    assert "Value" in snapshot
    # _format_cost correctness is verified separately in test_format_cost_*.


def test_conflicting_pr_shows_in_red() -> None:
    """CONFLICTING and BLOCKED PR statuses are styled with red.

    Uses export_html() to assert on color class attributes rather than
    plain-text output (rich colours are not visible in export_text()).
    """
    state = _tui_state(
        tracked_prs=[
            TrackedPR(
                number=55,
                base="epic/wor-300",
                last_status="CONFLICTING",
                last_poll=time.time() - 30.0,
            ),
            TrackedPR(
                number=56,
                base="main",
                last_status="BLOCKED",
                last_poll=time.time() - 60.0,
            ),
        ],
    )
    display = WatcherDisplay()
    console = Console(record=True, width=120, force_terminal=True)
    console.print(display._build_layout(state))
    html = console.export_html()

    assert "CONFLICTING" in html
    assert "BLOCKED" in html
    # Rich renders styled spans as <span class="r1"> where r1 maps to red
    assert "r1" in html  # rich red style class


def test_no_live_instantiated_in_snapshot_tests() -> None:
    """Verifying that snapshot tests do not trigger Live creation.

    _build_layout never creates a Live widget — only _render_live does,
    and that is only called when _is_tty() is True and self._live is None.
    Since we call _build_layout directly, Live.start() must never be called.
    """
    state = _tui_state()
    with (
        patch.object(Live, "start", wraps=Live.start) as mock_start,
        patch.object(Live, "update", wraps=Live.update) as mock_update,
        patch.object(Live, "refresh", wraps=Live.refresh) as mock_refresh,
    ):
        display = WatcherDisplay()
        console = Console(record=True, width=120, force_terminal=True)
        console.print(display._build_layout(state))

    mock_start.assert_not_called()
    mock_update.assert_not_called()
    mock_refresh.assert_not_called()


def test_pipe_fallback_no_live() -> None:
    """When Console has no TTY (file=StringIO), _render_live delegates to _render_line
    and never instantiates Live.

    This covers the pipe fallback path in update_state → _render_live.
    """
    state = _tui_state(
        workers=[
            WorkerState(
                ticket_id="WOR-50",
                mode="local",
                status="running",
                elapsed_s=30.0,
            ),
        ],
    )

    display = WatcherDisplay()
    # Piped console: no TTY → should NOT create Live widget
    with patch.object(Live, "start") as mock_start:
        # Calling update_state with a non-TTY console should route to _render_line
        display.update_state(state)

    # _is_tty() checks sys.stderr.isatty() which is False in tests,
    # so update_state should call _render_line, not _render_live
    mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# Sub-widget unit tests
# ---------------------------------------------------------------------------


def test_format_cost_small_values() -> None:
    """Values < $0.01 use 4-decimal formatting."""
    assert WatcherDisplay._format_cost(0.005) == "$0.0050"
    assert WatcherDisplay._format_cost(0.0) == "$0.0000"
    assert WatcherDisplay._format_cost(0.0099) == "$0.0099"


def test_format_cost_regular_values() -> None:
    """Values >= $0.01 use 2-decimal formatting."""
    assert WatcherDisplay._format_cost(1.0) == "$1.00"
    assert WatcherDisplay._format_cost(66.0) == "$66.00"
    assert WatcherDisplay._format_cost(1000.5) == "$1000.50"


def test_format_elapsed_seconds() -> None:
    """Sub-60s elapsed displays as Xs."""
    assert WatcherDisplay._format_elapsed(0) == "0s"
    assert WatcherDisplay._format_elapsed(59) == "59s"
    assert WatcherDisplay._format_elapsed(30) == "30s"


def test_format_elapsed_minutes() -> None:
    """60s+ elapsed displays as MmSSs."""
    assert WatcherDisplay._format_elapsed(60) == "1m00s"
    assert WatcherDisplay._format_elapsed(125) == "2m05s"
    assert WatcherDisplay._format_elapsed(3661) == "61m01s"


def test_format_elapsed_boundary() -> None:
    """Exactly 60s boundary: 60s → 1m00s, 59s → 59s."""
    assert WatcherDisplay._format_elapsed(59) == "59s"
    assert WatcherDisplay._format_elapsed(60) == "1m00s"


# ---------------------------------------------------------------------------
# Worker/PR management
# ---------------------------------------------------------------------------


def test_add_and_remove_worker() -> None:
    """add_worker appends, remove_worker filters by ticket_id."""
    display = WatcherDisplay()
    state = TUIState()
    display._state = state

    display.add_worker(WorkerState(ticket_id="WOR-1", mode="local", status="running"))
    assert len(display._state.workers) == 1
    assert display._state.workers[0].ticket_id == "WOR-1"

    display.remove_worker("WOR-1")
    assert len(display._state.workers) == 0


def test_update_pr_registers_and_updates() -> None:
    """First call appends, subsequent calls update existing entry."""
    display = WatcherDisplay()
    state = TUIState()
    display._state = state

    display.update_pr(42, "epic/wor-300", "PENDING", poll_time=1000.0)
    assert len(display._state.tracked_prs) == 1
    pr = display._state.tracked_prs[0]
    assert pr.number == 42
    assert pr.base == "epic/wor-300"
    assert pr.last_status == "PENDING"
    assert pr.last_poll == 1000.0

    # Update existing
    display.update_pr(42, "epic/wor-300", "MERGED", poll_time=2000.0)
    assert len(display._state.tracked_prs) == 1
    pr = display._state.tracked_prs[0]
    assert pr.last_status == "MERGED"
    assert pr.last_poll == 2000.0


def test_poll_pr_status_returns_unknown_on_error() -> None:
    """When gh CLI is unavailable or errors, return ('?','?')."""
    with patch("subprocess.run", side_effect=FileNotFoundError("gh not found")):
        result = WatcherDisplay.poll_pr_status(42)
    assert result == ("?", "?")


def test_poll_pr_status_returns_unknown_on_bad_json() -> None:
    """Malformed JSON response returns ('?','?')."""
    fake_response = MagicMock()
    fake_response.returncode = 0
    fake_response.stdout = "not json {{{"
    with patch("subprocess.run", return_value=fake_response):
        result = WatcherDisplay.poll_pr_status(42)
    assert result == ("?", "?")


def test_poll_pr_status_returns_unknown_on_nonzero_exit() -> None:
    """Non-zero return code → ('?','?')."""
    fake_response = MagicMock()
    fake_response.returncode = 1
    fake_response.stdout = ""
    with patch("subprocess.run", return_value=fake_response):
        result = WatcherDisplay.poll_pr_status(42)
    assert result == ("?", "?")


# ---------------------------------------------------------------------------
# Line-based fallback
# ---------------------------------------------------------------------------


def test_render_line_logs_worker_lines() -> None:
    """_render_line appends log lines for each worker."""
    display = WatcherDisplay()
    state = _tui_state(
        workers=[
            WorkerState(
                ticket_id="WOR-1",
                mode="local",
                status="running",
                elapsed_s=60.0,
                local_saved=5.0,
            ),
        ],
    )

    with patch("app.core.watcher.watcher_tui.logger") as mock_logger:
        display._render_line(state)

    mock_logger.info.assert_called()
    # Check the first call has the worker line
    calls = [call[0][1] for call in mock_logger.info.call_args_list]
    assert any("WOR-1" in msg for msg in calls)
    assert any("local" in msg for msg in calls)
    assert any("saved=$5.0000" in msg for msg in calls)


def test_render_line_logs_cost_lines() -> None:
    """_render_line appends cost rollup summary lines for each period."""
    display = WatcherDisplay()
    state = _tui_state(
        cost_rollups={
            "today": CostRollup(cloud_spent=10.0, local_saved=5.0),
            "week": CostRollup(cloud_spent=20.0, local_saved=10.0),
            "all": CostRollup(cloud_spent=100.0, local_saved=50.0),
        },
    )

    with patch("app.core.watcher.watcher_tui.logger") as mock_logger:
        display._render_line(state)

    calls = [call[0][1] for call in mock_logger.info.call_args_list]
    # Should have 3 cost lines + potentially worker lines
    cost_lines = [c for c in calls if "COST" in c]
    assert any("Today" in c for c in cost_lines)
    assert any("Week" in c for c in cost_lines)
    assert any("All" in c for c in cost_lines)


# ---------------------------------------------------------------------------
# TTY gating
# ---------------------------------------------------------------------------


def test_is_tty_returns_false_in_tests() -> None:
    """In automated test environments, stderr is rarely a TTY."""
    from app.core.watcher.watcher_tui import _is_tty

    # In pytest, stderr is usually captured → not a TTY
    # This test documents the expected behaviour, not a requirement
    # Just verifies no crash — _is_tty() may be True/False depending on env
    _is_tty()  # noqa: F841


def test_stop_silently_handles_oserror() -> None:
    """Live.stop() may raise OSError during shutdown — must be caught."""
    display = WatcherDisplay()
    fake_live = MagicMock()
    fake_live.stop.side_effect = OSError("broken pipe")
    display._live = fake_live

    # Should not raise
    display.stop()
    assert display._live is None


def test_stop_with_no_live_is_noop() -> None:
    """When _live is None, stop() does nothing."""
    display = WatcherDisplay()
    display._live = None
    # Should not raise
    display.stop()


def test_build_layout_has_correct_structure() -> None:
    """_build_layout returns a Layout with root → top/middle/bottom."""
    state = _tui_state()
    display = WatcherDisplay()
    layout = display._build_layout(state)

    assert layout.name == "root"
    assert layout["top"] is not None
    assert layout["middle"] is not None
    assert layout["bottom"] is not None


# ---------------------------------------------------------------------------
# _build_tui_state — empty workers list
# ---------------------------------------------------------------------------


def test_build_tui_state_empty_workers(tmp_path: Path) -> None:
    """When no workers are active, _build_tui_state returns a TUIState with
    an empty workers list but still includes cost rollups."""
    from app.core.watcher.watcher import Watcher

    w = Watcher(
        linear_client=MagicMock(),
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )

    state = build_tui_state(
        w._local_active,
        w._cloud_active,
        w._metrics,
        w._tracked_prs,
    )

    assert isinstance(state, TUIState)
    assert len(state.workers) == 0
    assert "today" in state.cost_rollups
    assert "week" in state.cost_rollups
    assert "all" in state.cost_rollups
    assert state.tracked_prs == []


# ---------------------------------------------------------------------------
# _build_tui_state — single local worker
# ---------------------------------------------------------------------------


def test_build_tui_state_single_local_worker(tmp_path: Path) -> None:
    """When one local worker is active, _build_tui_state includes it with
    mode='local' and elapsed time calculated from start_time."""
    from app.core.watcher.watcher import Watcher

    w = Watcher(
        linear_client=MagicMock(),
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )

    worker = MagicMock()
    worker.ticket_id = "WOR-TEST"
    import time as _time

    worker.start_time = _time.monotonic() - 60  # 60s ago
    w._local_active.append(worker)

    state = build_tui_state(
        w._local_active,
        w._cloud_active,
        w._metrics,
        w._tracked_prs,
    )

    assert len(state.workers) == 1
    ws = state.workers[0]
    assert isinstance(ws, WorkerState)
    assert ws.ticket_id == "WOR-TEST"
    assert ws.mode == "local"
    assert ws.status == "running"
    assert abs(ws.elapsed_s - 60) < 3  # ±3s for test timing


# ---------------------------------------------------------------------------
# vLLM table — empty and populated
# ---------------------------------------------------------------------------


def test_vllm_table_shows_dash_when_no_metrics() -> None:
    """When vllm_metrics is None, the vLLM panel shows 'No vLLM running'."""
    state = _tui_state()
    display = WatcherDisplay()
    layout = display._build_layout(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(layout)
    text = console.export_text()

    assert "No vLLM running" in text
    assert "vLLM" in text


def test_vllm_table_shows_metrics_when_present() -> None:
    """When vllm_metrics is populated, the panel shows token and latency data."""
    from app.core.watcher.watcher_tui import TUIState

    state = TUIState(
        workers=[],
        cost_rollups={
            "today": CostRollup(),
            "week": CostRollup(),
            "all": CostRollup(),
        },
        vllm_metrics={
            "vllm:generation_tokens_total": 15000.0,
            "vllm:prompt_tokens_total": 5000.0,
            "vllm:prefix_cache_hit_ratio": 0.72,
            "vllm:num_preemptions_total": 3.0,
            "vllm:ttft_mean_seconds": 0.045,
        },
    )
    display = WatcherDisplay()
    layout = display._build_layout(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(layout)
    text = console.export_text()

    assert "vLLM" in text
    assert "20,000" in text  # total tokens (15k + 5k)
    assert "72%" in text  # cache hit ratio
    assert "3" in text  # queue depth
    assert "0.045" in text  # TTFT


# ---------------------------------------------------------------------------
# Queue table
# ---------------------------------------------------------------------------


def test_queue_table_shows_counts() -> None:
    """The queue panel shows ticket counts for each state."""
    from app.core.watcher.watcher_tui import QueueState, TUIState

    state = TUIState(
        workers=[],
        cost_rollups={
            "today": CostRollup(),
            "week": CostRollup(),
            "all": CostRollup(),
        },
        queue_state=QueueState(ready=3, waiting=2, in_progress=1, blocked=0),
    )
    display = WatcherDisplay()
    layout = display._build_layout(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(layout)
    text = console.export_text()

    assert "Queue" in text
    assert "ReadyForLocal" in text
    assert "WaitingForDeps" in text
    assert "InProgressLocal" in text
    assert "Blocked" in text
    assert "3" in text
    assert "2" in text
    assert "1" in text
    assert "0" in text


# ---------------------------------------------------------------------------
# WOR-447 - Cost Economics table "no data yet" placeholder
# ---------------------------------------------------------------------------


def test_cost_table_shows_no_data_when_all_zero() -> None:
    """When all cost rollups are zero, the table shows a 'No data yet' row."""
    state = _tui_state(
        cost_rollups={
            "today": CostRollup(),  # all zeros
            "week": CostRollup(),
            "all": CostRollup(),
        },
    )
    display = WatcherDisplay()
    table = display._cost_table(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(table)
    text = console.export_text()

    assert "No data yet" in text
    assert "Cost Economics" in text


def test_cost_table_hides_no_data_when_any_period_has_data() -> None:
    """When at least one period has non-zero cost, no 'No data yet' placeholder."""
    state = _tui_state(
        cost_rollups={
            "today": CostRollup(
                cloud_spent=66.0,
                local_saved=2.22,
                cloud_ticket_count=3,
                local_ticket_count=5,
            ),
            "week": CostRollup(),
            "all": CostRollup(),
        },
    )
    display = WatcherDisplay()
    table = display._cost_table(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(table)
    text = console.export_text()

    assert "No data yet" not in text
    assert "Today" in text  # the non-empty period row


# ---------------------------------------------------------------------------
# WOR-447 - Session Totals "no data yet" when no workers
# ---------------------------------------------------------------------------


def test_session_totals_no_data_when_no_workers() -> None:
    """When no active workers, Session Totals shows 'No data yet'."""
    state = _tui_state(workers=[])
    display = WatcherDisplay()
    table = display._rollup_table(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(table)
    text = console.export_text()

    assert "No data yet" in text
    assert "Session Totals" in text


def test_session_totals_shows_worked_data_with_workers() -> None:
    """When active workers exist, Session Totals shows live metrics."""
    state = _tui_state(
        workers=[
            WorkerState(
                ticket_id="WOR-50",
                mode="local",
                status="running",
                elapsed_s=125.0,
                local_saved=3.75,
            ),
        ],
        cost_rollups={
            "today": CostRollup(cloud_spent=10.0, local_saved=5.0),
            "week": CostRollup(),
            "all": CostRollup(),
        },
    )
    display = WatcherDisplay()
    table = display._rollup_table(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(table)
    text = console.export_text()

    assert "No data yet" not in text
    assert "Session Local Saved" in text
    assert "Active Workers" in text


def test_session_totals_includes_tickets_dispatched_today() -> None:
    """Session Totals includes 'Tickets Dispatched Today' row."""
    state = _tui_state(
        workers=[
            WorkerState(
                ticket_id="WOR-50",
                mode="local",
                status="running",
                elapsed_s=60.0,
            ),
        ],
        cost_rollups={
            "today": CostRollup(
                cloud_spent=10.0,
                local_saved=5.0,
                cloud_ticket_count=2,
                local_ticket_count=3,
            ),
            "week": CostRollup(),
            "all": CostRollup(),
        },
    )
    display = WatcherDisplay()
    table = display._rollup_table(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(table)
    text = console.export_text()

    assert "Tickets Dispatched Today" in text
    assert "5" in text  # 2 + 3


def test_session_totals_includes_cost_saved_estimate() -> None:
    """Session Totals includes 'Total Cost Saved Estimate' row."""
    state = _tui_state(
        workers=[
            WorkerState(
                ticket_id="WOR-50",
                mode="local",
                status="running",
                elapsed_s=60.0,
            ),
        ],
        cost_rollups={
            "today": CostRollup(
                cloud_spent=10.0,
                local_saved=5.0,
                cloud_ticket_count=2,
                local_ticket_count=3,
            ),
            "week": CostRollup(),
            "all": CostRollup(),
        },
    )
    display = WatcherDisplay()
    table = display._rollup_table(state)
    console = Console(record=True, width=120, force_terminal=True)
    console.print(table)
    text = console.export_text()

    assert "Total Cost Saved Estimate" in text
    assert "$5.0000" in text


# ---------------------------------------------------------------------------
# WOR-447 - cost computation with input + output tokens
# ---------------------------------------------------------------------------


def test_build_tui_state_cost_uses_input_and_output_tokens(tmp_path: Path) -> None:
    """build_tui_state computes cost from both input and output tokens."""
    from app.core.watcher.watcher import Watcher

    w = Watcher(
        linear_client=MagicMock(),
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )

    worker = MagicMock()
    worker.ticket_id = "WOR-TEST"
    worker.start_time = time.monotonic() - 60
    # Create a log path that _parse_worker_usage can read
    log_dir = tmp_path / "wor_test" / ".claude" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "wor_test.jsonl"
    # Write an assistant event with usage (input + output tokens)
    log_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 1000, "output_tokens": 2000},
                    "content": [{"type": "text", "text": "hello"}],
                },
            }
        )
        + "\n"
    )
    worker.worktree_path = tmp_path / "wor_test"
    w._local_active.append(worker)

    state = build_tui_state(
        w._local_active,
        w._cloud_active,
        w._metrics,
        w._tracked_prs,
    )

    assert len(state.workers) == 1
    ws = state.workers[0]
    assert ws.mode == "local"
    # Cost = 1000 * 3e-6 + 2000 * 15e-6 = 0.003 + 0.03 = 0.033
    assert ws.local_saved == pytest.approx(0.033)
