"""Tests for app.core.watcher.watcher_heartbeat — idle-line, heartbeat,
TUI-state, and queue-count helpers extracted from watcher.py (WOR-414).

Bumps coverage from 38% → 90%+. The existing tests in test_watcher_tui.py
and test_watcher_worker_lifecycle.py cover build_tui_state and
emit_heartbeat at a high level; this file fills in the smaller helpers
and the edge branches.
"""

from __future__ import annotations

import logging
import subprocess
import time as _time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher_heartbeat import (
    _build_local_worker_log_path,
    _count_queue_items,
    _live_cost_estimate,
    emit_heartbeat,
    emit_idle_line,
)
from app.core.watcher.watcher_types import ActiveWorker

# ── Helpers ─────────────────────────────────────────────────────────────────


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
        "base_branch": "main",
        "worker_branch": "wor-10-test",
        "objective": "Do the thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)


def _make_active_worker(
    ticket_id: str = "WOR-HB",
    worktree_path: Path | None = None,
    elapsed_seconds: float = 0.0,
) -> ActiveWorker:
    manifest = _make_manifest(
        ticket_id=ticket_id,
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
    )
    w = ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=worktree_path or Path(f"/tmp/{ticket_id}"),
        process=MagicMock(spec=subprocess.Popen),
    )
    if elapsed_seconds > 0:
        w.start_time = _time.monotonic() - elapsed_seconds
    return w


def _write_manifest(art_dir: Path, ticket_id: str, status: str) -> None:
    """Write a minimal manifest.json under .claude/artifacts/<slug>/."""
    slug = ticket_id.lower().replace("-", "_")
    d = art_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    m = _make_manifest(ticket_id=ticket_id, status=status)
    (d / "manifest.json").write_text(m.model_dump_json(), encoding="utf-8")


# ── _live_cost_estimate ─────────────────────────────────────────────────────


def test_live_cost_estimate_none_returns_zero() -> None:
    """None output_tokens → 0.0."""
    assert _live_cost_estimate(None) == 0.0


def test_live_cost_estimate_zero_returns_zero() -> None:
    """0 output_tokens → 0.0 (avoid division by zero artifacts)."""
    assert _live_cost_estimate(0) == 0.0


def test_live_cost_estimate_scales_linearly() -> None:
    """Cost scales linearly at $15/M output tokens."""
    # 1M tokens at $15/M = $15
    assert _live_cost_estimate(1_000_000) == 15.0
    # 100k tokens → $1.50
    assert abs(_live_cost_estimate(100_000) - 1.50) < 1e-9


# ── _build_local_worker_log_path ────────────────────────────────────────────


def test_build_local_worker_log_path_normalises_ticket_id(tmp_path: Path) -> None:
    """Hyphen in ticket_id → underscore in JSONL filename; lives under
    <worktree>/.claude/logs/. Case is preserved."""
    w = _make_active_worker(ticket_id="WOR-123", worktree_path=tmp_path)
    p = _build_local_worker_log_path(w)
    assert p == tmp_path / ".claude" / "logs" / "WOR_123.jsonl"


# ── emit_idle_line ──────────────────────────────────────────────────────────


def test_emit_idle_line_emits_on_state_change(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """First emission (last_idle_state=None) always logs + returns new state."""
    with caplog.at_level(logging.INFO):
        state = emit_idle_line(
            now_local=0,
            now_cloud=0,
            max_local_workers=8,
            max_cloud_workers=3,
            repo_root=tmp_path,
            last_idle_state=None,
        )

    assert state == (0, 0, 0, True)
    assert any("Watcher idle" in r.message for r in caplog.records)


def test_emit_idle_line_skips_when_state_unchanged(tmp_path: Path) -> None:
    """Same state as last_idle_state → returns None, no log emitted."""
    prev = (0, 0, 0, True)
    state = emit_idle_line(
        now_local=0,
        now_cloud=0,
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
        last_idle_state=prev,
    )
    assert state is None


def test_emit_idle_line_counts_waiting_manifests(tmp_path: Path) -> None:
    """ExecutionManifest files with status=WaitingForDeps are counted."""
    art_root = tmp_path / ".claude" / "artifacts"
    _write_manifest(art_root, "WOR-1", status="WaitingForDeps")
    _write_manifest(art_root, "WOR-2", status="WaitingForDeps")
    _write_manifest(art_root, "WOR-3", status="ReadyForLocal")  # not counted

    state = emit_idle_line(
        now_local=0,
        now_cloud=0,
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
        last_idle_state=None,
    )

    assert state is not None
    assert state[2] == 2  # waiting count


def test_emit_idle_line_handles_unreadable_manifest(tmp_path: Path) -> None:
    """Corrupt manifest.json is skipped silently (doesn't poison the count)."""
    art_root = tmp_path / ".claude" / "artifacts"
    bad = art_root / "wor_bad"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("not valid json", encoding="utf-8")
    _write_manifest(art_root, "WOR-1", status="WaitingForDeps")

    state = emit_idle_line(
        now_local=0,
        now_cloud=0,
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
        last_idle_state=None,
    )

    assert state is not None
    assert state[2] == 1  # one waiting, one corrupt skipped


def test_emit_idle_line_no_capacity_state(tmp_path: Path) -> None:
    """When both pools are full, has_capacity=False is in the returned state."""
    state = emit_idle_line(
        now_local=8,
        now_cloud=3,
        max_local_workers=8,
        max_cloud_workers=3,
        repo_root=tmp_path,
        last_idle_state=None,
    )
    assert state == (8, 3, 0, False)


# ── emit_heartbeat — additional edge cases ──────────────────────────────────


def test_emit_heartbeat_skips_under_30s_first_emit() -> None:
    """First emission for a worker with elapsed < 30s does NOT populate
    heartbeat dict (avoids spurious zero-tick log)."""
    w = _make_active_worker(ticket_id="WOR-NEW", elapsed_seconds=15)
    heartbeat: dict[str, tuple[float, int]] = {}

    result = emit_heartbeat([w], [], heartbeat)

    assert "WOR-NEW" not in result


def test_emit_heartbeat_emits_for_cloud_worker() -> None:
    """Cloud workers are emitted the same way as local workers."""
    w = _make_active_worker(ticket_id="WOR-CLOUD", elapsed_seconds=45)
    heartbeat: dict[str, tuple[float, int]] = {}

    result = emit_heartbeat([], [w], heartbeat)

    assert "WOR-CLOUD" in result


# ── _count_queue_items ──────────────────────────────────────────────────────


def test_count_queue_items_combines_linear_and_manifests(tmp_path: Path) -> None:
    """ready/in_progress from Linear; waiting/blocked from manifest scan."""
    linear = MagicMock()
    linear.list_issues_by_state.side_effect = lambda state: {
        "ReadyForLocal": [object(), object()],  # 2
        "InProgressLocal": [object()],  # 1
    }.get(state, [])

    art_root = tmp_path / ".claude" / "artifacts"
    _write_manifest(art_root, "WOR-A", status="WaitingForDeps")
    _write_manifest(art_root, "WOR-B", status="WaitingForDeps")
    _write_manifest(art_root, "WOR-C", status="WaitingForDeps")
    _write_manifest(art_root, "WOR-D", status="Blocked")

    qs = _count_queue_items(linear, tmp_path)

    assert qs.ready == 2
    assert qs.in_progress == 1
    assert qs.waiting == 3
    assert qs.blocked == 1


def test_count_queue_items_handles_linear_failure(tmp_path: Path) -> None:
    """If Linear queries raise, return zero counts (don't crash the TUI)."""
    linear = MagicMock()
    linear.list_issues_by_state.side_effect = RuntimeError("Linear down")

    qs = _count_queue_items(linear, tmp_path)

    assert qs.ready == 0
    assert qs.in_progress == 0


def test_count_queue_items_no_artifacts_dir(tmp_path: Path) -> None:
    """When .claude/artifacts doesn't exist, waiting + blocked are zero."""
    linear = MagicMock()
    linear.list_issues_by_state.return_value = []

    qs = _count_queue_items(linear, tmp_path)

    assert qs.waiting == 0
    assert qs.blocked == 0


def test_count_queue_items_skips_unreadable_manifest(tmp_path: Path) -> None:
    """Corrupt manifest.json doesn't poison the count."""
    linear = MagicMock()
    linear.list_issues_by_state.return_value = []

    art_root = tmp_path / ".claude" / "artifacts"
    bad = art_root / "wor_bad"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    _write_manifest(art_root, "WOR-OK", status="WaitingForDeps")

    qs = _count_queue_items(linear, tmp_path)

    assert qs.waiting == 1
