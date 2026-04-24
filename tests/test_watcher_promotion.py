"""Tests for app.core.watcher._promote_waiting_tickets."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from app.core.linear_client import LinearError
from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher import Watcher
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_waiting_manifest(
    ticket_id: str = "WOR-46",
    blocked_by: list[str] | None = None,
    linear_id: str | None = "fake-linear-uuid",
    **overrides: Any,
) -> ExecutionManifest:
    return make_manifest(
        ticket_id=ticket_id,
        status="WaitingForDeps",
        linear_id=linear_id,
        blocked_by_tickets=blocked_by if blocked_by is not None else ["WOR-45"],
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
        **overrides,
    )


def _write_manifest(manifest: ExecutionManifest, artifacts_root: Path) -> Path:
    slug = manifest.ticket_id.lower().replace("-", "_")
    path = artifacts_root / slug / "manifest.json"
    return manifest.to_json(path)


def _make_watcher_with_mock_linear(
    tmp_path: Path, state_type_map: dict[str, str | None] | None = None
) -> tuple[Watcher, MagicMock]:
    mock_linear = MagicMock()
    if state_type_map is not None:
        mock_linear.get_issue_state_type.side_effect = lambda id_: state_type_map.get(
            id_
        )
    watcher = Watcher(linear_client=mock_linear, repo_root=tmp_path)
    return watcher, mock_linear


# ---------------------------------------------------------------------------
# _promote_waiting_tickets
# ---------------------------------------------------------------------------


def test_promote_all_blockers_completed_promotes_to_ready(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "completed"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "ReadyForLocal"
    mock_linear.set_state.assert_called_once_with("fake-linear-uuid", "ReadyForLocal")
    mock_linear.post_comment.assert_called_once()


def test_promote_blocker_not_done_skips(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "started"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "WaitingForDeps"
    mock_linear.set_state.assert_not_called()


def test_promote_partial_blockers_skips(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest(blocked_by=["WOR-45", "WOR-47"])
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "completed", "WOR-47": "started"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "WaitingForDeps"
    mock_linear.set_state.assert_not_called()


def test_promote_cancelled_blocker_moves_to_backlog(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "cancelled"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "Backlog"
    mock_linear.set_state.assert_called_once_with("fake-linear-uuid", "Backlog")
    comment_body: str = mock_linear.post_comment.call_args[0][1]
    assert "WOR-45" in comment_body
    assert "manual intervention" in comment_body


def test_promote_cancelled_blocker_does_not_promote_to_ready(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "cancelled"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status != "ReadyForLocal"


def test_promote_cancelled_blocker_no_linear_id_updates_disk_only(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest(linear_id=None)
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "cancelled"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "Backlog"
    mock_linear.set_state.assert_not_called()
    mock_linear.post_comment.assert_not_called()


def test_promote_empty_blocked_by_promotes_immediately(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest(blocked_by=[])
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(tmp_path)
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "ReadyForLocal"
    mock_linear.get_issue_state_type.assert_not_called()


def test_promote_skips_non_waiting_manifests(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    ready_manifest = make_manifest(status="ReadyForLocal")
    _write_manifest(ready_manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(tmp_path)
    watcher._promote_waiting_tickets()

    mock_linear.get_issue_state_type.assert_not_called()
    mock_linear.set_state.assert_not_called()


def test_promote_linear_fetch_error_treated_as_unsatisfied(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()
    _write_manifest(manifest, artifacts)

    mock_linear = MagicMock()
    mock_linear.get_issue_state_type.side_effect = LinearError("network failure")
    watcher = Watcher(linear_client=mock_linear, repo_root=tmp_path)
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "WaitingForDeps"
    mock_linear.set_state.assert_not_called()


def test_promote_writes_updated_manifest_to_disk(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()
    path = _write_manifest(manifest, artifacts)

    watcher, _ = _make_watcher_with_mock_linear(tmp_path, {"WOR-45": "completed"})
    watcher._promote_waiting_tickets()

    reloaded = ExecutionManifest.from_json(path)
    assert reloaded.status == "ReadyForLocal"
    assert reloaded.ticket_id == "WOR-46"


def test_promote_no_artifacts_root_no_error(tmp_path: Path) -> None:
    watcher, _ = _make_watcher_with_mock_linear(tmp_path)
    watcher._promote_waiting_tickets()


def test_promote_no_linear_id_updates_disk_only(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest(linear_id=None)
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "completed"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "ReadyForLocal"
    mock_linear.set_state.assert_not_called()
    mock_linear.post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# _promote_waiting_tickets — context_snippets cleared on promotion
# ---------------------------------------------------------------------------


def test_promote_toctou_state_change_between_checks(tmp_path: Path) -> None:
    """Single snapshot prevents TOCTOU: both checks use state from the same fetch.

    Without the snapshot fix the old code called get_issue_state_type twice for
    the same blocker — once in _find_cancelled_blocker and once in
    _all_blockers_satisfied.  If the blocker state changed between those two
    calls the results were inconsistent.

    With the snapshot, get_issue_state_type is called exactly once per blocker
    per poll cycle; both classification helpers operate on that same dict.
    """
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()  # WOR-46, blocked by WOR-45
    _write_manifest(manifest, artifacts)

    call_count = 0

    def state_side_effect(blocker_id: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "completed"
        return "cancelled"  # any second call would see a changed state

    mock_linear = MagicMock()
    mock_linear.get_issue_state_type.side_effect = state_side_effect
    watcher = Watcher(linear_client=mock_linear, repo_root=tmp_path)
    watcher._promote_waiting_tickets()

    # Snapshot guarantees exactly one API call per blocker
    assert mock_linear.get_issue_state_type.call_count == 1
    # The single snapshot value ("completed") is used by both checks → promoted
    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "ReadyForLocal"


def test_promote_clears_context_snippets(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest(
        context_snippets=["# app/core/foo.py:1-10\nsome code"]
    )
    _write_manifest(manifest, artifacts)

    watcher, _ = _make_watcher_with_mock_linear(tmp_path, {"WOR-45": "completed"})
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "ReadyForLocal"
    assert on_disk.context_snippets is None
