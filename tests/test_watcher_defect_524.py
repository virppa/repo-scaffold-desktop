"""Tests for WOR-524: infinite re-dispatch on permanent worktree-add-128
and pause-path hang that never resumes on sentinel removal."""

from __future__ import annotations

import time as _time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import Watcher


def _make_manifest(**overrides):
    defaults = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test ticket",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "routing": "local",
        "review_mode": "auto",
        "base_branch": "wor-96-local-worker-engine",
        "worker_branch": "wor-10-test-ticket",
        "objective": "Do the thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)


# ---------------------------------------------------------------------------
# Defect 1: worktree-add-128 → transition to Blocked
# ---------------------------------------------------------------------------


def test_dispatch_loop_transitions_to_blocked_on_exception(tmp_path: Path) -> None:
    """When _start_ticket raises (e.g. worktree-add exit-128), the dispatch
    loop transitions the ticket from ReadyForLocal to Blocked so it does not
    get infinitely re-dispatched."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]
    # Simulate ticket still in ReadyForLocal (never set to InProgressLocal
    # because create_worktree failed before the state was updated)
    linear_mock.get_current_state_name.return_value = "ReadyForLocal"

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    # safe_set_state is imported from watcher_finalize; patch it where it is
    # looked up (the watcher module namespace), not at the source module.
    with (
        patch.object(w, "_start_ticket", side_effect=RuntimeError("exit-128")),
        patch(
            "app.core.watcher.watcher.safe_set_state",
        ) as mock_safe_set,
    ):
        w._dispatch_next_ticket()

    # safe_set_state should have been called to transition to Blocked
    mock_safe_set.assert_called_once()
    call_args = mock_safe_set.call_args
    # (linear, linear_id, state, ticket_id)
    assert call_args[0][2] == "Blocked"
    # The linear_id is the ticket_id in this call site
    assert call_args[0][1] == "WOR-10"


def test_dispatch_loop_skips_non_readyForLocal(tmp_path: Path) -> None:
    """When the ticket is NOT in ReadyForLocal (e.g. already InProgressLocal
    or in some other state), the dispatch loop does NOT force-set Blocked."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]
    # Simulate the ticket already being InProgressLocal
    linear_mock.get_current_state_name.return_value = "InProgressLocal"

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    with patch.object(w, "_start_ticket", side_effect=RuntimeError("exit-128")):
        w._dispatch_next_ticket()

    # Should NOT call set_state because state is not ReadyForLocal
    linear_mock.set_state.assert_not_called()


def test_dispatch_loop_continues_after_exception(tmp_path: Path) -> None:
    """After failing one ticket, the dispatch loop should continue to dispatch
    other eligible tickets."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-id-10",
            "labels": {"nodes": []},
        },
        {
            "identifier": "WOR-20",
            "id": "fake-id-20",
            "labels": {"nodes": []},
        },
    ]
    # Configure get_current_state_name so the exception handler's
    # transition-to-Blocked path is taken.
    linear_mock.get_current_state_name.return_value = "ReadyForLocal"

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    call_count = [0]

    def _start_ticket_fail_ok(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("exit-128")

    with (
        patch.object(w, "_start_ticket", side_effect=_start_ticket_fail_ok),
        patch("app.core.watcher.watcher.safe_set_state") as mock_safe_set,
    ):
        w._dispatch_next_ticket()

    # Both tickets dispatched (fail + succeed) — dispatch loop continues
    assert call_count[0] == 2
    # WOR-10 should have been transitioned to Blocked via safe_set_state
    mock_safe_set.assert_called_once()
    set_state_args = mock_safe_set.call_args
    assert set_state_args[0][2] == "Blocked"


# ---------------------------------------------------------------------------
# Defect 2: pause-resume
# ---------------------------------------------------------------------------


def test_check_pause_resumption_resumes_when_sentinel_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the pause sentinel is removed, _check_pause_resumption should
    clear the paused flag and log."""
    import app.core.watcher.watcher_signals as _wsig

    # Create sentinel to trigger paused state
    sentinel = tmp_path / ".claude" / "watcher.pause"
    sentinel.parent.mkdir(parents=True)
    sentinel.touch()

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._paused = True
    w._paused_at = _time.monotonic()

    # Remove the sentinel
    sentinel.unlink()

    # Patch pause_sentinel_path to return the sentinel
    with patch.object(_wsig, "pause_sentinel_path", return_value=sentinel):
        w._check_pause_resumption()

    assert w._paused is False
    assert w._paused_at is None


def test_check_pause_resumption_no_op_when_not_paused(tmp_path: Path) -> None:
    """When not paused, _check_pause_resumption should be a no-op."""
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._paused = False

    w._check_pause_resumption()
    assert w._paused is False


def test_check_pause_resumption_no_op_when_sentinel_still_exists(
    tmp_path: Path,
) -> None:
    """When paused but the sentinel still exists, the state should be
    unchanged."""
    sentinel = tmp_path / ".claude" / "watcher.pause"
    sentinel.parent.mkdir(parents=True)
    sentinel.touch()

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._paused = True
    original_paused_at = _time.monotonic()
    w._paused_at = original_paused_at

    with patch(
        "app.core.watcher.watcher_signals.pause_sentinel_path",
        return_value=sentinel,
    ):
        w._check_pause_resumption()

    assert w._paused is True
    assert w._paused_at == original_paused_at


# ---------------------------------------------------------------------------
# Pause heartbeat
# ---------------------------------------------------------------------------


def test_pause_heartbeat_emits_periodic_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """While paused, _pause_heartbeat should emit a 'still paused (Ns)'
    message with elapsed seconds."""
    import logging

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._paused = True
    w._paused_at = _time.monotonic() - 25  # 25 seconds ago

    caplog.set_level(logging.INFO, logger="app.core.watcher.watcher")

    with caplog.at_level(logging.INFO, logger="app.core.watcher.watcher"):
        w._pause_heartbeat()

    # Log message: "Watcher daemon paused — still running (25s)"
    assert any("paused" in r.message and "running" in r.message for r in caplog.records)


def test_pause_heartbeat_no_op_when_not_paused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When not paused, _pause_heartbeat should not emit anything."""
    import logging

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._paused = False

    caplog.set_level(logging.INFO, logger="app.core.watcher.watcher")
    with caplog.at_level(logging.INFO, logger="app.core.watcher.watcher"):
        w._pause_heartbeat()

    assert not any("still paused" in r.message for r in caplog.records)


def test_pause_heartbeat_no_op_when_paused_at_is_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When paused but _paused_at is None, _pause_heartbeat should not emit."""
    import logging

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._paused = True
    w._paused_at = None

    caplog.set_level(logging.INFO, logger="app.core.watcher.watcher")
    with caplog.at_level(logging.INFO, logger="app.core.watcher.watcher"):
        w._pause_heartbeat()

    assert not any("still paused" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _check_pause_resumption integration
# ---------------------------------------------------------------------------


def test_resume_clears_all_pause_state(tmp_path: Path) -> None:
    """When the sentinel is removed, _check_pause_resumption should clear
    both _paused and _paused_at."""
    sentinel = tmp_path / ".claude" / "watcher.pause"
    # Do NOT create the sentinel — it should be absent
    assert not sentinel.exists()

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    w._paused = True
    w._paused_at = 1000.0

    with patch(
        "app.core.watcher.watcher_signals.pause_sentinel_path",
        return_value=sentinel,
    ):
        w._check_pause_resumption()

    assert w._paused is False
    assert w._paused_at is None
