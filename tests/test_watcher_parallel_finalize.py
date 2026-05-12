"""Tests for WOR-451: parallel finalize-checks queue.

Asserts:
  - Multiple worker finalize calls run concurrently in the ThreadPoolExecutor.
  - PR creation is serialised per base_branch (concurrent attempts to the
    same base wait for each other; different bases run in parallel).
  - max_concurrent_checks=1 reverts to fully serial behavior.
  - Default max_concurrent_checks = max(max_local_workers // 2, 2).
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import Watcher
from app.core.watcher.watcher_finalize import (
    _get_pr_lock,
    _pr_locks,
    attempt_pr,
)
from app.core.watcher.watcher_types import ActiveWorker


def _make_manifest(
    *, ticket_id: str = "WOR-10", base_branch: str = "epic/wor-96"
) -> ExecutionManifest:
    return ExecutionManifest(
        ticket_id=ticket_id,
        epic_id="WOR-96",
        title="Test",
        priority=2,
        status="ReadyForLocal",
        parallel_safe=True,
        risk_level="low",
        implementation_mode="local",
        review_mode="auto",
        base_branch=base_branch,
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        objective="Do the thing.",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
        allowed_paths=["app/core/foo.py"],
        required_checks=["pytest"],
    )


def _make_finished_worker(
    ticket_id: str, base_branch: str = "epic/wor-96", rc: int = 0
) -> ActiveWorker:
    manifest = _make_manifest(ticket_id=ticket_id, base_branch=base_branch)
    worker = ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=Path(f"/tmp/{ticket_id}"),
        process=MagicMock(spec=subprocess.Popen),
    )
    worker.process.poll.return_value = rc
    return worker


def test_default_max_concurrent_checks_is_half_of_local_workers() -> None:
    """Default pool size = max(max_local_workers // 2, 2)."""
    watcher = Watcher(linear_client=MagicMock(), max_local_workers=8)
    assert watcher._max_concurrent_checks == 4

    watcher2 = Watcher(linear_client=MagicMock(), max_local_workers=2)
    # max(2 // 2, 2) = max(1, 2) = 2
    assert watcher2._max_concurrent_checks == 2

    watcher3 = Watcher(linear_client=MagicMock(), max_local_workers=16)
    assert watcher3._max_concurrent_checks == 8


def test_explicit_max_concurrent_checks_overrides_default() -> None:
    watcher = Watcher(
        linear_client=MagicMock(),
        max_local_workers=8,
        max_concurrent_checks=1,
    )
    assert watcher._max_concurrent_checks == 1


def test_reap_pool_runs_finalizes_concurrently() -> None:
    """3 finished workers should finalize in parallel, not in series."""
    workers = [
        _make_finished_worker("WOR-301"),
        _make_finished_worker("WOR-302"),
        _make_finished_worker("WOR-303"),
    ]
    watcher = Watcher(
        linear_client=MagicMock(),
        max_local_workers=8,
        max_concurrent_checks=3,
    )

    # finalize_worker mock sleeps 200ms. Serial would be ~600ms;
    # parallel ~200ms + thread overhead.
    def slow_finalize(*args: Any, **kwargs: Any) -> str:
        time.sleep(0.2)
        return "success"

    with (
        patch("app.core.watcher.watcher.finalize_worker", side_effect=slow_finalize),
        patch(
            "app.core.watcher.watcher.format_worker_token_count",
            return_value="0 tokens",
        ),
    ):
        t0 = time.perf_counter()
        watcher._reap_pool(workers)
        elapsed = time.perf_counter() - t0

    assert workers == [], "All slots should be freed"
    # Ceiling 500ms — well below the 600ms serial baseline.
    assert elapsed < 0.5, f"Finalizes not concurrent: took {elapsed:.2f}s"


def test_reap_pool_serial_when_pool_size_is_one() -> None:
    """max_concurrent_checks=1 reverts to serial behavior."""
    workers = [
        _make_finished_worker("WOR-401"),
        _make_finished_worker("WOR-402"),
        _make_finished_worker("WOR-403"),
    ]
    watcher = Watcher(
        linear_client=MagicMock(),
        max_local_workers=8,
        max_concurrent_checks=1,
    )

    def slow_finalize(*args: Any, **kwargs: Any) -> str:
        time.sleep(0.15)
        return "success"

    with (
        patch("app.core.watcher.watcher.finalize_worker", side_effect=slow_finalize),
        patch(
            "app.core.watcher.watcher.format_worker_token_count",
            return_value="0 tokens",
        ),
    ):
        t0 = time.perf_counter()
        watcher._reap_pool(workers)
        elapsed = time.perf_counter() - t0

    assert workers == []
    # Serial: 3 × 0.15s = 0.45s minimum. Floor at 0.4s to allow scheduling slop.
    assert elapsed >= 0.4, f"Expected serial (~0.45s), got {elapsed:.2f}s"


def test_pr_lock_serialises_same_base_branch() -> None:
    """Two attempt_pr calls on the same base must serialise."""
    base = "epic/test-same-base-lock"
    # Wipe any cached lock from prior tests so we get a fresh one.
    _pr_locks.pop(base, None)

    times: dict[str, list[float]] = {"start": [], "end": []}
    times_lock = threading.Lock()

    def fake_create_pr(manifest: ExecutionManifest, worktree: Path) -> str:
        with times_lock:
            times["start"].append(time.perf_counter())
        time.sleep(0.15)
        with times_lock:
            times["end"].append(time.perf_counter())
        return "https://example.com/pr/1"

    worker1 = _make_finished_worker("WOR-501", base_branch=base)
    worker2 = _make_finished_worker("WOR-502", base_branch=base)
    linear = MagicMock()

    def run_attempt(worker: ActiveWorker) -> None:
        attempt_pr(worker.manifest, worker, linear)

    with patch(
        "app.core.watcher.watcher_finalize.create_pr",
        side_effect=fake_create_pr,
    ):
        t1 = threading.Thread(target=run_attempt, args=(worker1,))
        t2 = threading.Thread(target=run_attempt, args=(worker2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    # Serialised: t1 ends before t2 starts (or vice versa). I.e. there is no
    # interval where both are running.
    starts = sorted(times["start"])
    ends = sorted(times["end"])
    assert len(starts) == 2 and len(ends) == 2, (
        f"Expected 2 start/end pairs; got starts={starts} ends={ends}"
    )
    # First finishes before second starts.
    assert ends[0] <= starts[1] + 0.01, (
        f"Calls overlapped: ends={ends}, starts={starts}"
    )


def test_pr_lock_allows_different_base_branches_in_parallel() -> None:
    """Two attempt_pr calls on different bases must run in parallel."""
    base_a = "epic/test-different-base-a"
    base_b = "epic/test-different-base-b"
    _pr_locks.pop(base_a, None)
    _pr_locks.pop(base_b, None)

    barrier = threading.Barrier(2, timeout=5.0)

    def fake_create_pr(manifest: ExecutionManifest, worktree: Path) -> str:
        # Both threads must hit the barrier simultaneously; if the lock
        # were inadvertently shared they'd serialise and the barrier
        # would time out.
        barrier.wait()
        return "https://example.com/pr/1"

    worker_a = _make_finished_worker("WOR-601", base_branch=base_a)
    worker_b = _make_finished_worker("WOR-602", base_branch=base_b)
    linear = MagicMock()

    def run_attempt(worker: ActiveWorker) -> None:
        attempt_pr(worker.manifest, worker, linear)

    with patch(
        "app.core.watcher.watcher_finalize.create_pr",
        side_effect=fake_create_pr,
    ):
        t_a = threading.Thread(target=run_attempt, args=(worker_a,))
        t_b = threading.Thread(target=run_attempt, args=(worker_b,))
        t_a.start()
        t_b.start()
        t_a.join(timeout=3.0)
        t_b.join(timeout=3.0)

    # If both completed, the barrier was satisfied, meaning they ran
    # concurrently (different-base locks did not serialise them).
    assert not t_a.is_alive() and not t_b.is_alive(), (
        "Threads timed out — different-base PR calls did not parallelise"
    )


def test_get_pr_lock_returns_same_instance_for_same_base() -> None:
    base = "epic/test-cache-key"
    _pr_locks.pop(base, None)
    lock1 = _get_pr_lock(base)
    lock2 = _get_pr_lock(base)
    assert lock1 is lock2


def test_get_pr_lock_returns_distinct_for_different_bases() -> None:
    a = "epic/test-distinct-a"
    b = "epic/test-distinct-b"
    _pr_locks.pop(a, None)
    _pr_locks.pop(b, None)
    assert _get_pr_lock(a) is not _get_pr_lock(b)
