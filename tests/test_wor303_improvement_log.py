"""WOR-303 — improvement-log: auto-post result.json notes to WOR-254.

Verifies that finalize_worker reads the worker's result.json, extracts the
`notes` field, and posts a structured Linear comment to the WOR-254
improvement-log when the notes exceed the hardcoded 50-char threshold.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
from app.core.manifest import ArtifactPaths
from app.core.watcher.watcher_finalize import finalize_worker
from app.core.watcher.watcher_types import ActiveWorker
from app.core.watcher.watcher_worktrees import WipPreservationResult
from tests.conftest import make_manifest

_DEFAULT_PROJECT = "repo-scaffold-desktop"


def _write_result_json(repo_root: Path, ticket_id: str, **fields: object) -> Path:
    """Write a worker result.json under repo_root's artifact dir."""
    slug = ticket_id.lower().replace("-", "_")
    art = repo_root / ".claude" / "artifacts" / slug
    art.mkdir(parents=True, exist_ok=True)
    f = art / "result.json"
    payload = {"ticket_id": ticket_id, "status": "success", "summary": "done"}
    payload.update(fields)
    f.write_text(json.dumps(payload), encoding="utf-8")
    return f


def _make_worker(tmp_path: Path, ticket_id: str) -> ActiveWorker:
    """Build an ActiveWorker whose worktree_path lives under tmp_path."""
    manifest = make_manifest(
        ticket_id=ticket_id,
        worker_branch=f"{ticket_id.lower().replace('-', '')}-test-branch",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
    )
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path / "worktree",
        process=MagicMock(spec=subprocess.Popen),
    )


def test_post_comment_when_notes_exceed_50_chars(tmp_path: Path) -> None:
    """result.json notes > 50 chars → post_comment called with WOR-254."""
    long_notes = "x" * 51  # 51 chars > NOTES_MIN_CHARS
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_result_json(worktree, "WOR-303", notes=long_notes)
    worker = _make_worker(tmp_path, "WOR-303")
    metrics_mock = MagicMock()
    linear_mock = MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_finalize._execute_finalization",
            return_value=("success", False, True, None, [], "success"),
        ),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=metrics_mock,
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=worktree,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    linear_mock.post_comment.assert_called_once()
    call_args = linear_mock.post_comment.call_args
    assert call_args[0][0] == "WOR-254"
    body = call_args[0][1]
    assert "## Side-discovery from WOR-303" in body
    assert "Test ticket" in body  # manifest title
    assert long_notes in body
    assert "wor303-test-branch" in body


def test_no_post_when_notes_at_threshold(tmp_path: Path) -> None:
    """notes == 50 chars → NOT > 50, so no post_comment call."""
    at_threshold = "x" * 50  # exactly 50, NOT > 50
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_result_json(worktree, "WOR-303", notes=at_threshold)
    worker = _make_worker(tmp_path, "WOR-303")
    linear_mock = MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_finalize._execute_finalization",
            return_value=("success", False, True, None, [], "success"),
        ),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=worktree,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    linear_mock.post_comment.assert_not_called()


def test_no_post_when_notes_empty(tmp_path: Path) -> None:
    """Empty notes → no post_comment call (len("") <= 50)."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_result_json(worktree, "WOR-303", notes="")
    worker = _make_worker(tmp_path, "WOR-303")
    linear_mock = MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_finalize._execute_finalization",
            return_value=("success", False, True, None, [], "success"),
        ),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=worktree,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    linear_mock.post_comment.assert_not_called()


def test_no_post_when_result_json_missing(tmp_path: Path) -> None:
    """Missing result.json → no exception, no post_comment call."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # No result.json — file does not exist.
    worker = _make_worker(tmp_path, "WOR-303")
    linear_mock = MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_finalize._execute_finalization",
            return_value=("success", False, True, None, [], "success"),
        ),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=worktree,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    linear_mock.post_comment.assert_not_called()


def test_no_post_when_notes_field_absent(tmp_path: Path) -> None:
    """result.json without a `notes` field → no exception, no post_comment."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # Write result.json with no "notes" key.
    slug = "wor_303"
    art = worktree / ".claude" / "artifacts" / slug
    art.mkdir(parents=True, exist_ok=True)
    (art / "result.json").write_text(
        json.dumps({"ticket_id": "WOR-303", "status": "success", "summary": "done"}),
        encoding="utf-8",
    )
    worker = _make_worker(tmp_path, "WOR-303")
    linear_mock = MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_finalize._execute_finalization",
            return_value=("success", False, True, None, [], "success"),
        ),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=worktree,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    linear_mock.post_comment.assert_not_called()


def test_graceful_failure_when_linear_unreachable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Linear unreachable → logger.warning fires, finalize continues."""
    long_notes = "x" * 51
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_result_json(worktree, "WOR-303", notes=long_notes)
    worker = _make_worker(tmp_path, "WOR-303")
    metrics_mock = MagicMock()
    linear_mock = MagicMock()
    linear_mock.post_comment.side_effect = ConnectionRefusedError("connection refused")

    with (
        patch(
            "app.core.watcher.watcher_finalize._execute_finalization",
            return_value=("success", False, True, None, [], "success"),
        ),
        patch(
            "app.core.watcher.watcher_finalize.commit_wip_state",
            return_value=WipPreservationResult(
                status="clean", sha=None, backup_path=None, error=None
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        caplog.at_level(logging.WARNING, logger="app.core.watcher.watcher_finalize"),
    ):
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=metrics_mock,
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=worktree,
            mode="default",
            project_id=_DEFAULT_PROJECT,
        )

    # The hook warned but did not raise — finalize continued.
    assert any(
        "Could not post improvement-log comment" in r.message for r in caplog.records
    )
    # metrics still recorded — the failure didn't break the flow.
    metrics_mock.record.assert_called_once()
