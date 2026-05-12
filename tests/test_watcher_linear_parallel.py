"""Tests for WOR-469: parallel Linear API writes in finalize.

Asserts that `safe_set_state_and_comment` fires the underlying
`safe_set_state` and `_try_post_comment` calls concurrently in a
ThreadPoolExecutor, halving the blocking Linear API wait per pair.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

from app.core.watcher.watcher_finalize_helpers import (
    _get_linear_executor,
    safe_set_state_and_comment,
)


def test_safe_set_state_and_comment_runs_in_parallel() -> None:
    """Both Linear writes must overlap; total wall ≈ max(both)."""
    linear = MagicMock()
    starts: list[float] = []
    ends: list[float] = []

    def slow_set_state(*_args: Any, **_kwargs: Any) -> None:
        starts.append(time.perf_counter())
        time.sleep(0.2)
        ends.append(time.perf_counter())

    def slow_post_comment(*_args: Any, **_kwargs: Any) -> None:
        starts.append(time.perf_counter())
        time.sleep(0.2)
        ends.append(time.perf_counter())

    linear.set_state.side_effect = slow_set_state
    linear.post_comment.side_effect = slow_post_comment

    t0 = time.perf_counter()
    safe_set_state_and_comment(linear, "issue-id", "Blocked", "WOR-1", "some body")
    elapsed = time.perf_counter() - t0

    # Both methods called once each
    linear.set_state.assert_called_once()
    linear.post_comment.assert_called_once()

    # Serial: 400ms; parallel: ~200ms + small overhead. Ceiling 350ms.
    assert elapsed < 0.35, (
        f"Linear writes did not run in parallel: total={elapsed:.2f}s, expected < 0.35s"
    )

    # Both started before either finished — overlap proof.
    assert starts[0] < ends[0]
    assert starts[1] < ends[1]
    earlier_end = min(ends)
    later_start = max(starts)
    assert later_start < earlier_end, (
        f"Calls did not overlap: starts={starts}, ends={ends}"
    )


def test_safe_set_state_and_comment_both_called_even_on_state_error() -> None:
    """If set_state raises LinearError, post_comment still fires."""
    from app.core.linear_client import LinearError

    linear = MagicMock()
    linear.set_state.side_effect = LinearError("boom")

    safe_set_state_and_comment(linear, "issue-id", "Blocked", "WOR-1", "some body")

    linear.set_state.assert_called_once()
    linear.post_comment.assert_called_once()


def test_safe_set_state_and_comment_both_called_even_on_comment_error() -> None:
    """If post_comment raises, set_state still fires (and vice versa)."""
    linear = MagicMock()
    linear.post_comment.side_effect = RuntimeError("server down")

    # Should not propagate (existing wrapper semantics)
    safe_set_state_and_comment(linear, "issue-id", "Blocked", "WOR-1", "some body")

    linear.set_state.assert_called_once()
    linear.post_comment.assert_called_once()


def test_get_linear_executor_is_singleton() -> None:
    """Lazy-init returns the same instance across calls."""
    ex1 = _get_linear_executor()
    ex2 = _get_linear_executor()
    assert ex1 is ex2
