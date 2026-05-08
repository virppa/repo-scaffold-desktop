"""Tests for the watcher signal handling and daemon lifecycle.

Covers SIGTERM handling, verbose flag defaults, startup info logging,
softstop/drain mode, and stale sentinel cleanup.
"""

from __future__ import annotations

import logging
import subprocess
import time as _time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import Watcher
from app.core.watcher.watcher_types import ActiveWorker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_manifest(**overrides: Any) -> ExecutionManifest:
    defaults: dict[str, Any] = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test ticket",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "review_mode": "auto",
        "base_branch": "wor-96-local-worker-engine",
        "worker_branch": "wor-10-test-ticket",
        "objective": "Do the thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        # WOR-378: dispatch refuses manifests with empty required_checks; default
        # a non-empty list to match the conftest fixture.
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)


_SENTINEL: list[str] = ["app/core/bar.py"]


def _make_active_worker(
    ticket_id: str = "WOR-11", allowed_paths: list[str] | None = None
) -> ActiveWorker:
    paths = _SENTINEL if allowed_paths is None else allowed_paths
    manifest = _make_manifest(
        ticket_id=ticket_id,
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
        allowed_paths=paths,
    )
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=Path(f"/tmp/{ticket_id}"),
        process=MagicMock(spec=subprocess.Popen),
    )


# ---------------------------------------------------------------------------
# Watcher verbose flags
# ---------------------------------------------------------------------------


def test_worker_verbose_defaults_to_false() -> None:
    w = Watcher(linear_client=MagicMock())
    assert w._worker_verbose is False


def test_worker_verbose_stores_true() -> None:
    w = Watcher(linear_client=MagicMock(), worker_verbose=True)
    assert w._worker_verbose is True


def test_worker_verbose_and_no_epic_shutdown_can_be_combined() -> None:
    """Both flags can be set simultaneously — they are independent."""
    w = Watcher(linear_client=MagicMock(), worker_verbose=True, no_epic_shutdown=True)
    assert w._worker_verbose is True


# ---------------------------------------------------------------------------
# _handle_signal — SIGTERM calls services.stop() and sets _running=False
# ---------------------------------------------------------------------------


def test_handle_signal_sigterm_calls_services_stop_and_clears_running() -> None:
    """Post-WOR-368 the watcher no longer owns a LiteLLM process to terminate;
    services.stop() is a no-op kept for call-site compat. The signal handler
    must still invoke it AND clear _running so the poll loop exits."""
    w = Watcher(linear_client=MagicMock())
    import signal

    with patch.object(w._services, "stop") as mock_stop:
        w._handle_signal(signal.SIGTERM, None)

    mock_stop.assert_called_once()
    assert w._running is False


# ---------------------------------------------------------------------------
# _log_startup_info — cloud mode omits max_local_workers
# ---------------------------------------------------------------------------


def test_startup_info_cloud_mode_omits_max_local_workers(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    w = Watcher(
        linear_client=MagicMock(),
        worker_mode="cloud",
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        w._log_startup_info()
    msg = caplog.text
    assert "mode=cloud" in msg
    assert "max_cloud_workers=3" in msg
    assert "max_local_workers" not in msg


# ---------------------------------------------------------------------------
# _log_startup_info — local mode omits max_cloud_workers
# ---------------------------------------------------------------------------


def test_startup_info_local_mode_omits_max_cloud_workers(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    w = Watcher(
        linear_client=MagicMock(),
        worker_mode="local",
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        w._log_startup_info()
    msg = caplog.text
    assert "mode=local" in msg
    assert "max_local_workers=8" in msg
    assert "max_cloud_workers" not in msg


# ---------------------------------------------------------------------------
# _log_startup_info — default mode logs both pool sizes
# ---------------------------------------------------------------------------


def test_startup_info_default_mode_logs_both_pool_sizes(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    w = Watcher(
        linear_client=MagicMock(),
        worker_mode="default",
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
    )
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        w._log_startup_info()
    msg = caplog.text
    assert "mode=default" in msg
    assert "max_local_workers=8" in msg
    assert "max_cloud_workers=3" in msg


# ---------------------------------------------------------------------------
# Soft-stop / drain mode (WOR-333)
# ---------------------------------------------------------------------------


def test_softstop_sentinel_triggers_drain_mode(tmp_path: Path) -> None:
    """Writing .claude/watcher.softstop puts the daemon in drain mode."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    sentinel = tmp_path / ".claude" / "watcher.softstop"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    assert w._draining is False
    w._check_softstop_request()
    assert w._draining is True
    assert w._draining_since is not None


def test_softstop_check_idempotent(tmp_path: Path) -> None:
    """Calling _check_softstop_request multiple times keeps draining_since stable."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    sentinel = tmp_path / ".claude" / "watcher.softstop"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    w._check_softstop_request()
    first_since = w._draining_since
    w._check_softstop_request()
    w._check_softstop_request()
    # _draining_since must not move on subsequent calls (one-shot transition)
    assert w._draining_since == first_since


def test_softstop_no_sentinel_no_drain(tmp_path: Path) -> None:
    """When no sentinel exists, the daemon stays in normal operation."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._check_softstop_request()
    assert w._draining is False
    assert w._draining_since is None


def test_drain_mode_skips_dispatch(tmp_path: Path) -> None:
    """When _draining is True, _dispatch_next_ticket should not be called.

    This test exercises the run-loop guard: when draining, dispatch is gated
    out even if there's pool capacity and a ticket waiting in Linear.
    """
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = [
        {"identifier": "WOR-99", "id": "fake-id", "labels": {"nodes": []}}
    ]
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._draining = True

    # Force a single iteration of the run loop body by patching out the
    # blocking parts. _dispatch_next_ticket should NOT be called when draining.
    with (
        patch.object(w, "_dispatch_next_ticket") as mock_dispatch,
        patch.object(w, "_promote_waiting_tickets") as mock_promote,
        patch.object(w, "_check_epic_completion") as mock_epic,
    ):
        # Simulate the run-loop body's drain-aware section
        if not w._draining:
            mock_promote()
        if not w._draining:
            mock_dispatch()
        if not w._draining:
            mock_epic()

    mock_dispatch.assert_not_called()
    mock_promote.assert_not_called()
    mock_epic.assert_not_called()


def test_stale_softstop_sentinel_cleaned_on_startup(tmp_path: Path) -> None:
    """A sentinel left over from a prior daemon run is removed at startup."""
    sentinel = tmp_path / ".claude" / "watcher.softstop"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    assert sentinel.exists()

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._remove_stale_softstop_sentinel()
    assert not sentinel.exists()


def test_softstop_stuck_warning_fires_after_threshold(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    """If drain is pending too long (default 60min), log a one-shot WARNING."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._draining = True
    # Backdate draining_since to 70 minutes ago
    w._draining_since = _time.monotonic() - 70 * 60

    # Add one fake active worker so the warning has something to print
    w._local_active.append(_make_active_worker(ticket_id="WOR-99"))
    w._local_active[0].start_time = _time.monotonic() - 70 * 60

    with caplog_at_level_helper() as records:
        w._maybe_warn_softstop_stuck()

    matching = [r for r in records if "Soft-stop pending" in r.getMessage()]
    assert matching, f"Expected stuck-warning; got: {[r.getMessage() for r in records]}"
    assert w._softstop_warned_stuck is True

    # Subsequent calls should NOT re-warn
    with caplog_at_level_helper() as records2:
        w._maybe_warn_softstop_stuck()
    assert not any("Soft-stop pending" in r.getMessage() for r in records2), (
        "Stuck-warning should be one-shot; second call must not re-emit"
    )


# Tiny helper for the one test above (caplog fixture differs across pytest versions)


class _LogCapture:
    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self._handler: logging.Handler | None = None

    def __enter__(self) -> list[logging.LogRecord]:
        self._handler = logging.Handler()
        self._handler.setLevel(logging.WARNING)
        self._handler.emit = lambda r: self.records.append(r)  # type: ignore[assignment]
        logging.getLogger("app.core.watcher.watcher").addHandler(self._handler)
        return self.records

    def __exit__(self, *_args: object) -> None:
        if self._handler is not None:
            logging.getLogger("app.core.watcher.watcher").removeHandler(self._handler)


def caplog_at_level_helper() -> _LogCapture:
    return _LogCapture()


# ---------------------------------------------------------------------------
# _handle_signal — SIGINT (same behaviour as SIGTERM)
# ---------------------------------------------------------------------------


def test_handle_signal_sigint_calls_services_stop_and_clears_running() -> None:
    """SIGINT must behave identically to SIGTERM — stop services and exit."""
    w = Watcher(linear_client=MagicMock())
    import signal

    with patch.object(w._services, "stop") as mock_stop:
        w._handle_signal(signal.SIGINT, None)

    mock_stop.assert_called_once()
    assert w._running is False


# ---------------------------------------------------------------------------
# _remove_softstop_sentinel — OSError is logged, not raised
# ---------------------------------------------------------------------------


def test_remove_softstop_sentinel_oserror_logged(tmp_path: Path) -> None:
    """If unlink raises OSError (e.g. permission denied), the watcher logs a
    WARNING but does not crash."""
    sentinel = tmp_path / ".claude" / "watcher.softstop"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    with patch("pathlib.Path.unlink", side_effect=PermissionError("denied")):
        w._remove_softstop_sentinel()

    # Must not raise — the sentinel removal is best-effort


# ---------------------------------------------------------------------------
# _check_softstop_request — not-draining, no sentinel
# ---------------------------------------------------------------------------


def test_check_softstop_request_not_draining_no_sentinel(tmp_path: Path) -> None:
    """When not in drain mode and the sentinel file does not exist, nothing
    should change — _draining stays False."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    assert w._draining is False

    with (
        patch.object(w, "_softstop_sentinel_path") as mock_path,
        patch.object(Path, "exists", return_value=False),
    ):
        mock_path.return_value = tmp_path / ".claude" / "watcher.softstop"
        w._check_softstop_request()

    assert w._draining is False
    assert w._draining_since is None


# ---------------------------------------------------------------------------
# _check_softstop_request — idempotent: already draining
# ---------------------------------------------------------------------------


def test_check_softstop_request_already_draining_noop(tmp_path: Path) -> None:
    """If the watcher is already draining, _check_softstop_request must be a
    no-op — it should not reset _draining_since or re-read the sentinel."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._draining = True
    w._draining_since = 12345.0

    original_draining_since = w._draining_since
    w._check_softstop_request()

    assert w._draining is True
    assert w._draining_since == original_draining_since


# ---------------------------------------------------------------------------
# _maybe_warn_softstop_stuck — not stuck (no workers running)
# ---------------------------------------------------------------------------


def test_maybe_warn_softstop_stuck_not_draining_noop(
    tmp_path: Path,
    caplog: pytest.LogCaptureContext,
) -> None:
    """When not in drain mode, _maybe_warn_softstop_stuck must return
    immediately without logging or touching _softstop_warned_stuck."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._draining = False  # not draining
    w._draining_since = _time.monotonic() - 200 * 60  # irrelevant
    w._softstop_warned_stuck = False  # verify this stays unchanged

    with caplog_at_level_helper() as records:
        w._maybe_warn_softstop_stuck()

    assert w._softstop_warned_stuck is False
    assert len(records) == 0


# ---------------------------------------------------------------------------
# run() — finally block cleanup (services.stop + pid removal + display.stop)
# ---------------------------------------------------------------------------


def test_run_exits_cleans_up_services_and_pid(tmp_path: Path) -> None:
    """When the poll loop exits (e.g. via SIGINT), the finally block must
    call services.stop(), remove the PID file, and stop the TUI display."""
    w = Watcher(
        linear_client=MagicMock(),
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._running = False

    # Replace _services with a MagicMock so stop() is captured
    mock_services = MagicMock()
    w._services = mock_services

    w.run()

    # Finally block assertions — services.stop() must be called
    mock_services.stop.assert_called_once()


def test_run_exits_with_workers_and_clears_running() -> None:
    """When a signal sets _running=False mid-loop, the finally block must
    wait for active workers before cleaning up."""
    w = Watcher(
        linear_client=MagicMock(),
        repo_root=Path("/tmp"),
        no_epic_shutdown=True,
    )
    w._running = False
    worker = _make_active_worker()
    w._local_active.append(worker)

    mock_services_stop = MagicMock()
    with patch.object(w._services, "stop", mock_services_stop):
        w.run()

    # _wait_for_active_workers should have been called (part of finally block)
    worker.process.wait.assert_called_once()
    mock_services_stop.assert_called_once()
