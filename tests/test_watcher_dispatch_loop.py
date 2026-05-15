"""Tests for the watcher dispatch loop — _dispatch_next_ticket and related helpers.

Unit tests for the ticket-dispatching path in app.core.watcher: spike-label guard,
vLLM readiness gate, epic-completion memoization, manifest blockers, and per-type
concurrency (cloud pool full does not block local).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import ANY, MagicMock, patch

import pytest

from app.core.linear_client import LinearError
from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import Watcher, _ProcessedTicket
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
        "routing": "local",
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
# _dispatch_next_ticket — Spike label guard
# ---------------------------------------------------------------------------


def _spike_ticket(label_name: str = "Spike") -> dict[str, Any]:
    return {
        "id": "fake-linear-id",
        "identifier": "WOR-99",
        "title": "Some spike",
        "labels": {"nodes": [{"name": label_name}]},
    }


def _regular_ticket() -> dict[str, Any]:
    return {
        "id": "fake-linear-id",
        "identifier": "WOR-99",
        "title": "Regular ticket",
        "labels": {"nodes": [{"name": "local-ready"}]},
    }


def test_dispatch_skips_spike_labelled_ticket(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [_spike_ticket("Spike")]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with (
        patch.object(w, "_start_ticket") as mock_start,
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        w._dispatch_next_ticket()

    mock_start.assert_not_called()
    assert any("Spike" in msg and "WOR-99" in msg for msg in caplog.messages)


@pytest.mark.parametrize("label_name", ["spike", "SPIKE", "Spike"])
def test_dispatch_skips_spike_label_case_insensitive(
    tmp_path: Path, label_name: str
) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [_spike_ticket(label_name)]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with patch.object(w, "_start_ticket") as mock_start:
        w._dispatch_next_ticket()

    mock_start.assert_not_called()


def test_dispatch_proceeds_for_non_spike_ticket(tmp_path: Path) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [_regular_ticket()]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with patch.object(w, "_start_ticket") as mock_start:
        w._dispatch_next_ticket()

    mock_start.assert_called_with("WOR-99", "fake-linear-id", candidate=ANY)


def test_dispatch_missing_labels_field_no_crash(tmp_path: Path) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [
        {"id": "fake-linear-id", "identifier": "WOR-99", "title": "No labels"}
    ]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with patch.object(w, "_start_ticket") as mock_start:
        w._dispatch_next_ticket()

    mock_start.assert_called_with("WOR-99", "fake-linear-id", candidate=ANY)


# ---------------------------------------------------------------------------
# Per-type concurrency — cloud pool full does not block local dispatch
# ---------------------------------------------------------------------------


def test_cloud_pool_full_does_not_block_local_dispatch(tmp_path: Path) -> None:
    """A saturated cloud pool must not prevent a local ticket from being dispatched."""
    local_manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
        allowed_paths=["app/core/local_only.py"],
    )
    cloud_manifest = _make_manifest(
        ticket_id="WOR-99",
        worker_branch="wor-99-cloud-ticket",
        implementation_mode="cloud",
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-99"),
        allowed_paths=["app/core/cloud_only.py"],
    )

    mock_linear = MagicMock()
    mock_linear.get_open_blockers.return_value = []

    watcher = Watcher(
        linear_client=mock_linear,
        max_local_workers=1,
        max_cloud_workers=1,
    )

    watcher._cloud_active.append(
        ActiveWorker(
            ticket_id="WOR-99",
            linear_id="fake-cloud-id",
            manifest=cloud_manifest,
            worktree_path=tmp_path,
            process=MagicMock(spec=subprocess.Popen),
        )
    )

    fake_local_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(watcher, "_load_manifest", return_value=local_manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch.object(watcher._services, "ensure_vllm_anthropic_mode"),
        patch.object(watcher._services, "probe_vllm_health"),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_local_process,
        ),
    ):
        watcher._start_ticket("WOR-10", "fake-local-id")

    assert len(watcher._local_active) == 1
    assert watcher._local_active[0].ticket_id == "WOR-10"
    assert len(watcher._cloud_active) == 1
    assert watcher._cloud_active[0].ticket_id == "WOR-99"


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — vLLM Anthropic-mode gate (WOR-368)
# ---------------------------------------------------------------------------


def test_dispatch_calls_ensure_vllm_anthropic_mode_for_local_effective_mode(
    tmp_path: Path,
) -> None:
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {"identifier": "WOR-10", "id": "fake-linear-id", "labels": {"nodes": []}}
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.watcher_worktrees.create_worktree", return_value=tmp_path
        ),
        patch("app.core.watcher.watcher_worktrees.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher_worktrees.write_worker_pytest_config"),
        patch("app.core.watcher.watcher_finalize.safe_set_state"),
        patch("app.core.watcher.watcher_worktrees.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher_subprocess.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode") as mock_vllm_anthropic,
        patch.object(w._services, "probe_vllm_health") as mock_probe,
    ):
        w._dispatch_next_ticket()

    mock_vllm_anthropic.assert_called_once()
    mock_probe.assert_called_once()


def test_dispatch_skips_ensure_for_cloud_effective_mode(tmp_path: Path) -> None:
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="cloud",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {"identifier": "WOR-10", "id": "fake-linear-id", "labels": {"nodes": []}}
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.watcher_worktrees.create_worktree", return_value=tmp_path
        ),
        patch("app.core.watcher.watcher_worktrees.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher_worktrees.write_worker_pytest_config"),
        patch("app.core.watcher.watcher_finalize.safe_set_state"),
        patch("app.core.watcher.watcher_worktrees.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher_subprocess.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode") as mock_vllm_anthropic,
        patch.object(w._services, "probe_vllm_health") as mock_probe,
    ):
        w._dispatch_next_ticket()

    mock_vllm_anthropic.assert_not_called()
    mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — vLLM readiness gate: health probe blocks dispatch
# ---------------------------------------------------------------------------


def test_dispatch_deferred_when_vllm_not_ready(tmp_path: Path) -> None:
    """When probe_vllm_health() returns False, _dispatch_next_ticket must return
    without calling create_worktree, copy_manifest_to_worktree,
    write_worker_pytest_config, safe_set_state, or launch_worker.
    The ticket stays in ReadyForLocal."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.dispatch.create_worktree") as mock_create,
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state") as mock_set_state,
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "probe_vllm_health", return_value=False),
    ):
        w._dispatch_next_ticket()

    # Nothing should have been created — the ticket stays in ReadyForLocal
    mock_create.assert_not_called()
    mock_set_state.assert_not_called()
    # launch_worker should not have been called either
    fake_process.assert_not_called()


def test_dispatch_proceeds_when_vllm_ready(tmp_path: Path) -> None:
    """When probe_vllm_health() returns True, dispatch proceeds normally
    (create_worktree is called, state is set, worker is launched)."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="local",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.dispatch.create_worktree", return_value=tmp_path
        ) as mock_create,
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "probe_vllm_health", return_value=True),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
    ):
        w._dispatch_next_ticket()

    # create_worktree must be called — dispatch proceeded
    mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# _dispatch_next_ticket — cloud mode skips vLLM probe entirely
# ---------------------------------------------------------------------------


def test_cloud_mode_skips_vllm_probe(tmp_path: Path) -> None:
    """When effective mode is cloud, probe_vllm_health() must NOT be called.
    Dispatch proceeds directly to create_worktree."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        implementation_mode="cloud",
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.list_ready_for_local.return_value = [
        {
            "identifier": "WOR-10",
            "id": "fake-linear-id",
            "labels": {"nodes": []},
        }
    ]

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path, worker_mode="default")
    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch(
            "app.core.watcher.watcher_worktrees.create_worktree", return_value=tmp_path
        ),
        patch("app.core.watcher.watcher_worktrees.copy_manifest_to_worktree"),
        patch("app.core.watcher.watcher_worktrees.write_worker_pytest_config"),
        patch("app.core.watcher.watcher_finalize.safe_set_state"),
        patch("app.core.watcher.watcher_worktrees.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.watcher_subprocess.launch_worker",
            return_value=fake_process,
        ),
        patch.object(
            w._services, "probe_vllm_health", return_value=False
        ) as mock_probe,
    ):
        w._dispatch_next_ticket()

    # probe_vllm_health must not have been called for cloud mode
    mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# Manifest blocker check — dispatch skips when manifest declares an unmerged blocker
# ---------------------------------------------------------------------------


def test_dispatch_skipped_when_manifest_declares_unmerged_blocker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dispatch must be skipped when the manifest declares a blocker that is not yet
    in a Linear completed state, even if Linear's own blockedBy is empty."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        blocked_by_tickets=["WOR-266"],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_issue_state_type.return_value = "started"  # not done

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        caplog.at_level(logging.INFO, logger="app.core.watcher"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    # No worktree should have been created — the ticket stays in ReadyForLocal
    assert w._local_active == []
    assert any(
        "WOR-266" in msg and "unmerged blocker" in msg for msg in caplog.messages
    )


def test_dispatch_proceeds_when_manifest_blocked_by_tickets_is_empty(
    tmp_path: Path,
) -> None:
    """Dispatch must proceed when the manifest declares no blocked_by_tickets."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        blocked_by_tickets=[],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_issue_state_type.return_value = "started"

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = []

    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-10"


def test_dispatch_proceeds_when_all_manifest_blockers_are_merged(
    tmp_path: Path,
) -> None:
    """Dispatch must proceed when every manifest-declared blocker has reached
    a Linear completed state (MergedToEpic / Done -> state type 'completed')."""
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        blocked_by_tickets=["WOR-266", "WOR-267"],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_issue_state_type.side_effect = lambda x: "completed"  # both done

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = []

    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-10"


# ---------------------------------------------------------------------------
# _safe_set_state — daemon survives LinearError at _start_ticket
# ---------------------------------------------------------------------------


def test_start_ticket_set_state_failure_worker_still_starts(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.set_state.side_effect = LinearError("unknown state")

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.create_worktree"),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health"),
    ):
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-10"


# ---------------------------------------------------------------------------
# _enrich_with_retry_context — injects constraint when last_failure.json exists
# ---------------------------------------------------------------------------


def test_enrich_with_retry_context_injects_constraint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import json

    manifest = _make_manifest(implementation_constraints=["original constraint"])
    artifact_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    artifact_dir.mkdir(parents=True)
    failure = {
        "failed_at": "2026-04-30T10:00:00Z",
        "check": "pytest",
        "stdout": (
            "FAILED tests/test_watcher_worktrees.py"
            "::test_cleanup_orphaned_worktrees_removes_subdirs"
            " - AssertionError: assert 0 == 2\n"
        ),
        "stderr": "",
    }
    (artifact_dir / "last_failure.json").write_text(
        json.dumps(failure), encoding="utf-8"
    )

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    with caplog.at_level(logging.INFO, logger="app.core.watcher"):
        enriched = w._enrich_with_retry_context(manifest)

    assert enriched.implementation_constraints[0].startswith("RETRY:")
    assert "pytest" in enriched.implementation_constraints[0]
    assert (
        "test_cleanup_orphaned_worktrees_removes_subdirs"
        in (enriched.implementation_constraints[0])
    )
    assert enriched.implementation_constraints[1] == "original constraint"
    assert any("retry context" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# _enrich_with_retry_context — no-op when last_failure.json absent
# ---------------------------------------------------------------------------


def test_enrich_with_retry_context_noop_without_failure_file(tmp_path: Path) -> None:
    manifest = _make_manifest(implementation_constraints=["original constraint"])
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)
    enriched = w._enrich_with_retry_context(manifest)

    assert enriched.implementation_constraints == ["original constraint"]


# ---------------------------------------------------------------------------
# _check_epic_completion — all-complete and nothing-processed paths
# ---------------------------------------------------------------------------


def test_check_epic_completion_posts_comment_and_exits(tmp_path: Path) -> None:
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    with (
        patch("app.core.watcher.watcher_epic.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(stdout="https://github.com/org/repo/pull/1")
        w._check_epic_completion()

    linear_mock.post_comment.assert_called_once_with(
        "WOR-96",
        "All sub-tickets merged — ready for `/close-epic WOR-96`",
    )
    assert w._running is False


def test_check_epic_completion_no_epic_shutdown_keeps_running(
    tmp_path: Path,
) -> None:
    """When no_epic_shutdown=True, _check_epic_completion must NOT set
    _running to False. The epic-complete comment must still be posted."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    with (
        patch("app.core.watcher.watcher_epic.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(stdout="https://github.com/org/repo/pull/1")
        w._check_epic_completion()

    linear_mock.post_comment.assert_called_once()
    assert w._running is True


def test_check_epic_completion_no_tickets_processed_no_comment_exits(
    tmp_path: Path,
) -> None:
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    w._check_epic_completion()

    linear_mock.post_comment.assert_not_called()
    assert w._running is True


def test_check_epic_completion_empty_startup_keeps_polling(tmp_path: Path) -> None:
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    assert not w._processed_tickets

    w._check_epic_completion()

    assert w._running is True


# ---------------------------------------------------------------------------
# _check_epic_completion — memoization: duplicate suppression
# ---------------------------------------------------------------------------


def test_epic_completion_memoization_suppresses_duplicate(
    tmp_path: Path,
) -> None:
    """Two consecutive _check_epic_completion calls with identical state must
    post the Linear comment exactly once, not twice."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    w._check_epic_completion()
    w._check_epic_completion()

    linear_mock.post_comment.assert_called_once()


def test_epic_completion_re_emit_on_state_change(
    tmp_path: Path,
) -> None:
    """When the processed-ticket state changes between calls (e.g. a new ticket
    is added), a fresh comment IS posted."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
        )
    ]

    w._check_epic_completion()
    # State changed — add a second ticket
    w._processed_tickets.append(
        _ProcessedTicket(
            ticket_id="WOR-11",
            epic_id="WOR-96",
            worker_branch="wor-11-test-ticket",
            elapsed=60.0,
        )
    )
    w._check_epic_completion()

    assert linear_mock.post_comment.call_count == 2


def test_epic_completion_failed_ticket_blocks_comment(
    tmp_path: Path,
) -> None:
    """A _ProcessedTicket with succeeded=False must still block the comment
    and NOT trigger re-posts through memoization."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(
        linear_client=linear_mock,
        repo_root=tmp_path,
        no_epic_shutdown=True,
    )
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
            succeeded=False,
        )
    ]

    w._check_epic_completion()
    w._check_epic_completion()

    linear_mock.post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# _check_epic_completion — partial failure: at least one succeeded=False
# ---------------------------------------------------------------------------


def test_epic_completion_partial_failure_skips_comment(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    """When _processed_tickets contains at least one entry with succeeded=False,
    _check_epic_completion must log a WARNING and NOT call linear.post_comment.
    The watcher must not set _running=False."""
    linear_mock = MagicMock()
    linear_mock.list_ready_for_local.return_value = []
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)
    w._processed_tickets = [
        _ProcessedTicket(
            ticket_id="WOR-10",
            epic_id="WOR-96",
            worker_branch="wor-10-test-ticket",
            elapsed=120.0,
            succeeded=True,
        ),
        _ProcessedTicket(
            ticket_id="WOR-11",
            epic_id="WOR-96",
            worker_branch="wor-11-test-ticket",
            elapsed=60.0,
            succeeded=False,
        ),
    ]

    with caplog.at_level(logging.WARNING, logger="app.core.watcher"):
        w._check_epic_completion()

    # The epic-complete comment must NOT be posted when there's a failure
    linear_mock.post_comment.assert_not_called()
    # watcher still exits — _running is set to False regardless of success/failure
    assert w._running is False
    # A warning must be logged about the failure
    assert any(
        "failed" in msg.lower() and "succeeded" in msg.lower()
        for msg in caplog.messages
    )


# ---------------------------------------------------------------------------
# WOR-419 — epic-branch overlap gate in _start_ticket
# ---------------------------------------------------------------------------


def test_start_ticket_blocks_when_another_epic_branch_in_flight(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    """Dispatch to a new epic/* branch is refused when another epic/* is
    already in-flight on a local worker. A Linear comment is posted."""

    new_epic_manifest = _make_manifest(
        ticket_id="WOR-419",
        worker_branch="wor-419-epic-branch",
        base_branch="epic/wor-419-new-epic",
        allowed_paths=["app/core/new.py"],
    )
    active_epic_manifest = _make_manifest(
        ticket_id="WOR-335-A",
        worker_branch="wor-335-active",
        base_branch="epic/wor-335-active",
        allowed_paths=["app/core/active.py"],
    )

    w = Watcher(
        linear_client=MagicMock(),
        repo_root=tmp_path,
    )
    w._linear.get_open_blockers.return_value = []

    # Add an active worker on a different epic branch
    w._local_active.append(
        ActiveWorker(
            ticket_id="WOR-335-A",
            linear_id="fake-335",
            manifest=active_epic_manifest,
            worktree_path=tmp_path / "worktree_335",
            process=MagicMock(spec=subprocess.Popen),
        )
    )

    with (
        patch.object(w, "_load_manifest", return_value=new_epic_manifest),
        patch("app.core.watcher.dispatch.create_worktree"),
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        w._start_ticket("WOR-419", "fake-419")

    assert len(w._local_active) == 1  # original worker preserved, no new one
    assert w._local_active[0].ticket_id == "WOR-335-A"
    # Gate should have logged and posted a Linear comment
    assert any("epic branch" in m and "already in-flight" in m for m in caplog.messages)
    w._linear.post_comment.assert_called_once()


def test_start_ticket_proceeds_for_same_epic_branch(
    tmp_path: Path,
) -> None:
    """Dispatch within the SAME epic branch proceeds normally — the gate
    only blocks DIFFERENT epic branches."""

    same_epic_manifest = _make_manifest(
        ticket_id="WOR-419",
        worker_branch="wor-419-same-epic",
        base_branch="epic/wor-335-active",
        allowed_paths=["app/core/new.py"],
    )
    active_epic_manifest = _make_manifest(
        ticket_id="WOR-335-A",
        worker_branch="wor-335-active",
        base_branch="epic/wor-335-active",
        allowed_paths=["app/core/active.py"],
    )

    w = Watcher(
        linear_client=MagicMock(),
        repo_root=tmp_path,
    )
    w._linear.get_open_blockers.return_value = []

    w._local_active.append(
        ActiveWorker(
            ticket_id="WOR-335-A",
            linear_id="fake-335",
            manifest=active_epic_manifest,
            worktree_path=tmp_path / "worktree_335",
            process=MagicMock(spec=subprocess.Popen),
        )
    )

    with (
        patch.object(w, "_load_manifest", return_value=same_epic_manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=MagicMock(spec=subprocess.Popen),
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health", return_value=True),
    ):
        w._start_ticket("WOR-419", "fake-419")

    assert len(w._local_active) == 2  # both workers
    linear_mock = w._linear
    linear_mock.post_comment.assert_not_called()


def test_start_ticket_unaffected_when_no_epic_workers(
    tmp_path: Path,
) -> None:
    """A non-epic base_branch dispatches normally when no epic workers are active."""
    main_manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-main",
        base_branch="main",
        allowed_paths=["app/core/foo.py"],
    )

    w = Watcher(
        linear_client=MagicMock(),
        repo_root=tmp_path,
    )
    w._linear.get_open_blockers.return_value = []

    with (
        patch.object(w, "_load_manifest", return_value=main_manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=MagicMock(spec=subprocess.Popen),
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health", return_value=True),
    ):
        w._start_ticket("WOR-10", "fake-10")

    assert len(w._local_active) == 1
    w._linear.post_comment.assert_not_called()


# ---------------------------------------------------------------------------
# WOR-458 — InProgressLocal state lock guard
# ---------------------------------------------------------------------------


def test_start_ticket_blocked_when_in_progress_local(
    tmp_path: Path, caplog: pytest.LogCaptureContext
) -> None:
    """Dispatch must NOT proceed when the ticket's Linear state is
    InProgressLocal — even if stale artifacts exist and the ticket
    was re-dispatched by a race condition."""

    manifest = _make_manifest(
        ticket_id="WOR-458",
        worker_branch="wor-458-branch",
        base_branch="epic/wor-461-watcher-contract-wave-3",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_current_state_name.return_value = "InProgressLocal"
    linear_mock.list_ready_for_local.return_value = []

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        w._start_ticket("WOR-458", "fake-linear-id")

    assert len(w._local_active) == 0
    assert any(
        "InProgressLocal" in m and "Double-launch guard" in m for m in caplog.messages
    )
    # No worktree or state transition should occur
    w._linear.get_current_state_name.assert_called_once_with("fake-linear-id")


def test_start_ticket_proceeds_for_ready_for_local(
    tmp_path: Path,
) -> None:
    """When Linear state is ReadyForLocal, dispatch proceeds normally
    and stale artifacts are cleaned up."""
    manifest = _make_manifest(
        ticket_id="WOR-458",
        worker_branch="wor-458-branch",
        base_branch="epic/wor-461-watcher-contract-wave-3",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_current_state_name.return_value = "ReadyForLocal"

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    fake_process = MagicMock(spec=subprocess.Popen)
    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health", return_value=True),
    ):
        w._start_ticket("WOR-458", "fake-458")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-458"


def test_start_ticket_proceeds_when_state_not_found(
    tmp_path: Path,
) -> None:
    """If get_current_state_name returns None (issue not found),
    dispatch proceeds — the ticket is safe to re-dispatch."""
    manifest = _make_manifest(
        ticket_id="WOR-458",
        worker_branch="wor-458-branch",
        base_branch="epic/wor-461-watcher-contract-wave-3",
        allowed_paths=["app/core/foo.py"],
    )
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.get_current_state_name.return_value = None

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    fake_process = MagicMock(spec=subprocess.Popen)
    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch(
            "app.core.watcher.dispatch.launch_worker",
            return_value=fake_process,
        ),
        patch.object(w._services, "ensure_vllm_anthropic_mode"),
        patch.object(w._services, "probe_vllm_health", return_value=True),
    ):
        w._start_ticket("WOR-458", "fake-458")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-458"
