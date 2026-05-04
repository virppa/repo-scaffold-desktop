"""Tests for finalize_worker exception guards and direct safety functions."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from app.core.linear_client import LinearError
from app.core.watcher.watcher_finalize import _try_post_comment

# ---------------------------------------------------------------------------
# _try_post_comment exception guard
# ---------------------------------------------------------------------------


def test_try_post_comment_swallows_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exception in post_comment is logged as warning (does not raise)."""
    linear_mock = MagicMock()
    linear_mock.post_comment.side_effect = Exception("connection reset by peer")

    with caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"):
        _try_post_comment(linear_mock, "lin-id", "WOR-10", "some comment body")

    assert any("Could not post comment" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# safe_set_state — direct (AC: LinearError caught and logged as warning)
# ---------------------------------------------------------------------------


def test_safe_set_state_linear_error_logged_as_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LinearError is caught and logged as warning (does not raise)."""
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("network timeout")

    with caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"):
        # Should NOT raise — catches LinearError internally
        from app.core.watcher.watcher_finalize import safe_set_state

        safe_set_state(linear_mock, "fake-linear-id", "Blocked", "WOR-10")

    # set_state was called but the exception was caught and not re-raised
    linear_mock.set_state.assert_called_once_with("fake-linear-id", "Blocked")
    assert any("set_state failed" in msg for msg in caplog.messages)


def test_safe_set_state_success_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Successful set_state produces no warning log."""
    linear_mock = MagicMock()
    with caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"):
        from app.core.watcher.watcher_finalize import safe_set_state

        safe_set_state(linear_mock, "fake-linear-id", "In Progress", "WOR-10")

    assert not caplog.text or "set_state failed" not in caplog.text
    linear_mock.set_state.assert_called_once_with("fake-linear-id", "In Progress")
