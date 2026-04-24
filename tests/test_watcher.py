"""Tests for the watcher/orchestrator pure logic functions.

Integration tests (actually launching subprocesses, Linear API) are out of scope;
this file covers the unit-testable, I/O-free helpers.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.linear_client import LinearError
from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher import (
    ActiveWorker,
    Watcher,
    _parse_worker_usage,
    _tee_worker_output,
    build_worker_cmd,
    build_worker_env,
    check_allowed_paths_overlap,
    is_watcher_running,
    resolve_effective_mode,
)

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
# check_allowed_paths_overlap
# ---------------------------------------------------------------------------


def test_overlap_when_paths_share_entry() -> None:
    active = [_make_active_worker("WOR-11", allowed_paths=["app/core/foo.py"])]
    candidate = _make_manifest(allowed_paths=["app/core/foo.py"])
    conflicts = check_allowed_paths_overlap(active, candidate)
    assert conflicts == ["WOR-11"]


def test_no_overlap_when_paths_are_disjoint() -> None:
    active = [_make_active_worker("WOR-11", allowed_paths=["app/core/bar.py"])]
    candidate = _make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == []


def test_empty_candidate_paths_conflicts_with_all() -> None:
    active = [_make_active_worker("WOR-11", allowed_paths=["app/core/bar.py"])]
    candidate = _make_manifest(allowed_paths=[])
    conflicts = check_allowed_paths_overlap(active, candidate)
    assert conflicts == ["WOR-11"]


def test_empty_active_paths_conflicts_with_candidate() -> None:
    active = [_make_active_worker("WOR-11", allowed_paths=[])]
    candidate = _make_manifest(allowed_paths=["app/core/foo.py"])
    conflicts = check_allowed_paths_overlap(active, candidate)
    assert conflicts == ["WOR-11"]


def test_multiple_active_partial_overlap() -> None:
    active = [
        _make_active_worker("WOR-11", allowed_paths=["app/core/foo.py"]),
        _make_active_worker("WOR-12", allowed_paths=["app/core/baz.py"]),
    ]
    candidate = _make_manifest(allowed_paths=["app/core/foo.py"])
    conflicts = check_allowed_paths_overlap(active, candidate)
    assert conflicts == ["WOR-11"]


# ---------------------------------------------------------------------------
# build_worker_env
# ---------------------------------------------------------------------------


def test_cloud_mode_strips_base_url() -> None:
    base = {
        "ANTHROPIC_BASE_URL": "http://localhost:8082",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["PATH"] == "/usr/bin"


def test_cloud_mode_strips_model_var() -> None:
    base = {"ANTHROPIC_MODEL": "qwen3-coder:30b", "PATH": "/usr/bin"}
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_MODEL" not in env


def test_local_mode_injects_base_url() -> None:
    base = {"PATH": "/usr/bin"}
    env = build_worker_env("local", base)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8082"


def test_default_mode_passes_env_unchanged() -> None:
    base = {"ANTHROPIC_BASE_URL": "http://localhost:8082", "PATH": "/usr/bin"}
    env = build_worker_env("default", base)
    assert env == base


def test_cloud_mode_does_not_inject_base_url_if_absent() -> None:
    base = {"PATH": "/usr/bin"}
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_BASE_URL" not in env


# ---------------------------------------------------------------------------
# build_worker_cmd
# ---------------------------------------------------------------------------


def test_cloud_cmd_has_no_model_flag(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path)
    assert "--model" not in cmd
    assert "/implement-ticket WOR-10" in " ".join(cmd)


def test_local_cmd_includes_model_flag(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path)
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "qwen3-coder:30b"


def test_cmd_includes_dangerously_skip_permissions(tmp_path: Path) -> None:
    for mode in ("cloud", "local"):
        cmd = build_worker_cmd("WOR-10", mode, tmp_path)
        assert "--dangerously-skip-permissions" in cmd


def test_cmd_bare_mode_uses_worktree_path(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path)
    assert "--bare" in cmd
    idx = cmd.index("--add-dir")
    assert cmd[idx + 1] == str(tmp_path)


def test_cloud_cmd_has_no_bare_flag(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path)
    assert "--bare" not in cmd


def test_cmd_disallowed_tools_appended(tmp_path: Path) -> None:
    tools = ["Read(*watcher.py)", "Read(*metrics.py)"]
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=tools)
    assert "--disallowed-tools" in cmd
    idx = cmd.index("--disallowed-tools")
    assert cmd[idx + 1] == "Read(*watcher.py),Read(*metrics.py)"


def test_cmd_no_disallowed_tools_when_none(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=None)
    assert "--disallowed-tools" not in cmd


def test_build_snippet_tool_restrictions_extracts_basenames() -> None:
    from app.core.watcher import Watcher

    snippets = [
        "# app/core/watcher.py lines 574-589\nsome code",
        "# app/core/metrics.py lines 1-20\nmore code",
        "# app/core/watcher.py lines 600-620\nduplicate file",
    ]
    patterns = Watcher._build_snippet_tool_restrictions(snippets)
    assert patterns == ["Read(*watcher.py)", "Read(*metrics.py)"]


def test_build_snippet_tool_restrictions_ignores_malformed() -> None:
    from app.core.watcher import Watcher

    snippets = ["no header here", "# missing path\ncode"]
    patterns = Watcher._build_snippet_tool_restrictions(snippets)
    assert patterns == []


# ---------------------------------------------------------------------------
# resolve_effective_mode
# ---------------------------------------------------------------------------


def test_worker_mode_overrides_manifest_local() -> None:
    assert resolve_effective_mode("cloud", "local") == "cloud"


def test_worker_mode_overrides_manifest_cloud() -> None:
    assert resolve_effective_mode("local", "cloud") == "local"


def test_default_defers_to_manifest() -> None:
    assert resolve_effective_mode("default", "local") == "local"
    assert resolve_effective_mode("default", "cloud") == "cloud"


def test_default_hybrid_becomes_cloud() -> None:
    assert resolve_effective_mode("default", "hybrid") == "cloud"


# ---------------------------------------------------------------------------
# is_watcher_running
# ---------------------------------------------------------------------------


def test_is_watcher_running_no_pid_file(tmp_path: Path) -> None:
    pid_file = tmp_path / "watcher.pid"
    assert not is_watcher_running(pid_file)


def test_is_watcher_running_stale_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "watcher.pid"
    pid_file.write_text("9999999", encoding="utf-8")  # very unlikely to be real
    # Should return False (process not running) or True on very unlucky collision;
    # just verify no exception is raised
    result = is_watcher_running(pid_file)
    assert isinstance(result, bool)


def test_is_watcher_running_own_pid(tmp_path: Path) -> None:
    pid_file = tmp_path / "watcher.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    assert is_watcher_running(pid_file)


# ---------------------------------------------------------------------------
# Watcher._cleanup_orphaned_worktrees
# ---------------------------------------------------------------------------


def test_cleanup_orphaned_worktrees_removes_dirs(tmp_path: Path) -> None:
    worktree_dir = tmp_path.parent / "worktrees/wor-99-old-ticket"
    worktree_dir.mkdir(parents=True)

    mock_linear = MagicMock()
    watcher = Watcher(
        linear_client=mock_linear,
        repo_root=tmp_path,
    )

    with patch.object(watcher, "_cleanup_worktree") as mock_cleanup:
        watcher._cleanup_orphaned_worktrees()
        mock_cleanup.assert_called_once_with(worktree_dir)


def test_cleanup_orphaned_worktrees_skips_when_base_absent(tmp_path: Path) -> None:
    mock_linear = MagicMock()
    watcher = Watcher(linear_client=mock_linear, repo_root=tmp_path)
    # No exception — base dir simply doesn't exist
    watcher._cleanup_orphaned_worktrees()


# ---------------------------------------------------------------------------
# Watcher PID file
# ---------------------------------------------------------------------------


def test_write_and_remove_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / ".claude/watcher.pid"
    monkeypatch.setattr("app.core.watcher._PID_FILE", pid_file)

    mock_linear = MagicMock()
    watcher = Watcher(linear_client=mock_linear, repo_root=tmp_path)
    watcher._write_pid_file()
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8") == str(os.getpid())

    watcher._remove_pid_file()
    assert not pid_file.exists()


# ---------------------------------------------------------------------------
# _tee_worker_output
# ---------------------------------------------------------------------------


class _CaptureSink:
    """Byte sink that accumulates writes and tracks close without discarding data."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, b: bytes) -> int:
        self.data += b
        return len(b)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_tee_writes_prefixed_lines_to_dest() -> None:
    import io

    pipe = io.BytesIO(b"hello\nsecond line\n")
    log_sink: _CaptureSink = _CaptureSink()
    dest_sink = io.BytesIO()

    _tee_worker_output(pipe, log_sink, b"[WOR-62] ", dest_sink)  # type: ignore[arg-type]

    assert log_sink.data == b"hello\nsecond line\n"
    assert dest_sink.getvalue() == b"[WOR-62] hello\n[WOR-62] second line\n"


def test_tee_closes_log_file() -> None:
    import io

    pipe = io.BytesIO(b"line\n")
    log_sink = _CaptureSink()
    dest_sink = io.BytesIO()

    _tee_worker_output(pipe, log_sink, b"", dest_sink)  # type: ignore[arg-type]

    assert log_sink.closed


def test_tee_empty_pipe() -> None:
    import io

    pipe = io.BytesIO(b"")
    log_sink = _CaptureSink()
    dest_sink = io.BytesIO()

    _tee_worker_output(pipe, log_sink, b"[X] ", dest_sink)  # type: ignore[arg-type]

    assert log_sink.data == b""
    assert dest_sink.getvalue() == b""


# ---------------------------------------------------------------------------
# Watcher verbose flag
# ---------------------------------------------------------------------------


def test_watcher_verbose_defaults_to_false() -> None:
    w = Watcher(linear_client=MagicMock())
    assert w._verbose is False


def test_watcher_stores_verbose_true() -> None:
    w = Watcher(linear_client=MagicMock(), verbose=True)
    assert w._verbose is True


# ---------------------------------------------------------------------------
# _create_pr — push before gh pr create
# ---------------------------------------------------------------------------


def test_create_pr_pushes_branch_before_gh_pr(tmp_path: Path) -> None:
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        base_branch="main",
        title="Test ticket",
        done_definition="It works.",
    )
    call_order: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        if cmd[:2] == ["git", "push"]:
            call_order.append("push")
        elif cmd[:3] == ["gh", "pr", "create"]:
            call_order.append("gh_pr")
        result = MagicMock()
        result.stdout = "https://github.com/example/pr/1"
        return result

    w = Watcher(linear_client=MagicMock())
    with patch("app.core.watcher.subprocess.run", side_effect=fake_run):
        w._create_pr(manifest, tmp_path)

    assert call_order == ["push", "gh_pr"]


def test_create_pr_logs_warning_on_auto_merge_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        base_branch="main",
        title="Test ticket",
        done_definition="It works.",
    )
    pr_url = "https://github.com/example/pr/1"

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if cmd[:3] == ["gh", "pr", "merge"]:
            result.returncode = 1
            result.stderr = "auto-merge is not enabled for this repository"
            result.stdout = ""
        elif cmd[:3] == ["gh", "pr", "create"]:
            result.stdout = pr_url
        elif cmd[:2] == ["git", "log"]:
            result.stdout = "abc1234 some commit"
        else:
            result.stdout = pr_url
        return result

    w = Watcher(linear_client=MagicMock())
    with (
        patch("app.core.watcher.subprocess.run", side_effect=fake_run),
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        returned_url = w._create_pr(manifest, tmp_path)

    assert returned_url == pr_url
    assert any(
        "gh pr merge --auto failed" in msg
        and pr_url in msg
        and "rc=1" in msg
        and "auto-merge is not enabled" in msg
        for msg in caplog.messages
    )


# ---------------------------------------------------------------------------
# _finalize_worker — PR creation failure marks ticket Blocked, no crash
# ---------------------------------------------------------------------------


def test_finalize_worker_pr_failure_marks_blocked(tmp_path: Path) -> None:
    manifest = _make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        base_branch="main",
    )
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    exc = subprocess.CalledProcessError(1, "gh", stderr="Head sha can't be blank")

    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_create_pr", side_effect=exc),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        # Must not raise
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    linear_mock.post_comment.assert_called_once()
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "WOR-10" in comment_body
    assert "Head sha can't be blank" in comment_body
    metrics_mock.record.assert_called_once()


# ---------------------------------------------------------------------------
# retry_count wiring in _finalize_worker
# ---------------------------------------------------------------------------


def test_finalize_worker_retry_count_zero_on_success(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    call_kwargs = metrics_mock.record.call_args[0][0]
    assert call_kwargs.retry_count == 0


def test_finalize_worker_retry_count_increments_on_check_failure(
    tmp_path: Path,
) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())

    # Simulate two check-failure cycles by calling _finalize_worker twice with
    # the same worker (increments retry_count each time checks fail).
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch.object(w, "_run_checks", return_value=False),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics"),
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    assert worker.retry_count == 2


def test_finalize_worker_retry_count_two_failures_then_success(
    tmp_path: Path,
) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    # Two failures then success
    check_results = [False, False, True]
    with (
        patch.object(w, "_run_checks", side_effect=check_results),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)
        w._finalize_worker(worker, returncode=0, wall_time=1.0)
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    call_kwargs = metrics_mock.record.call_args[0][0]
    assert call_kwargs.retry_count == 2


# ---------------------------------------------------------------------------
# _safe_set_state — daemon survives LinearError at all set_state sites
# ---------------------------------------------------------------------------


def test_finalize_worker_set_state_failure_nonzero_no_crash(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("rate limit")
    w = Watcher(linear_client=linear_mock)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics"),
    ):
        # returncode != 0 triggers set_state(failed) — must not propagate
        w._finalize_worker(worker, returncode=1, wall_time=1.0)


def test_finalize_worker_set_state_failure_success_path_no_crash(
    tmp_path: Path,
) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.set_state.side_effect = LinearError("network error")
    w = Watcher(linear_client=linear_mock)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics"),
    ):
        # Success path: PR created; Linear automation handles MergedToEpic on merge
        w._finalize_worker(worker, returncode=0, wall_time=1.0)


def test_start_ticket_set_state_failure_worker_still_starts(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    linear_mock.get_open_blockers.return_value = []
    linear_mock.set_state.side_effect = LinearError("unknown state")

    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    fake_process = MagicMock(spec=subprocess.Popen)

    with (
        patch.object(w, "_load_manifest", return_value=manifest),
        patch.object(w, "_create_worktree", return_value=tmp_path),
        patch.object(w, "_copy_manifest_to_worktree"),
        patch.object(w, "_launch_worker", return_value=fake_process),
    ):
        # set_state raises — worker must still be launched and added to _local_active
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._local_active) == 1
    assert w._local_active[0].ticket_id == "WOR-10"


# ---------------------------------------------------------------------------
# _promote_waiting_tickets
# ---------------------------------------------------------------------------


def _make_waiting_manifest(
    ticket_id: str = "WOR-46",
    blocked_by: list[str] | None = None,
    linear_id: str | None = "fake-linear-uuid",
    **overrides: Any,
) -> ExecutionManifest:
    return _make_manifest(
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


def test_promote_cancelled_blocker_counts_as_done(tmp_path: Path) -> None:
    artifacts = tmp_path / ".claude" / "artifacts"
    manifest = _make_waiting_manifest()
    _write_manifest(manifest, artifacts)

    watcher, mock_linear = _make_watcher_with_mock_linear(
        tmp_path, {"WOR-45": "cancelled"}
    )
    watcher._promote_waiting_tickets()

    on_disk = ExecutionManifest.from_json(artifacts / "wor_46" / "manifest.json")
    assert on_disk.status == "ReadyForLocal"


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
    ready_manifest = _make_manifest(status="ReadyForLocal")
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
    watcher._promote_waiting_tickets()  # should not raise


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
# _preserve_worker_artifacts
# ---------------------------------------------------------------------------


def test_preserve_worker_artifacts_copies_log_and_result(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)

    worktree = tmp_path / "worktrees" / "wor-10"
    worktree.mkdir(parents=True)

    log_src = worktree / ".claude" / "worker_wor-10.log"
    log_src.parent.mkdir(parents=True)
    log_src.write_text("log content")

    result_src = worktree / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_src.parent.mkdir(parents=True)
    result_src.write_text('{"status": "success"}')

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )
    w._preserve_worker_artifacts(worker)

    artifact_dir = tmp_path / ".claude" / "artifacts" / "wor_10"
    assert (artifact_dir / "worker_wor-10.log").read_text() == "log content"
    assert (artifact_dir / "result.json").read_text() == '{"status": "success"}'


def test_preserve_worker_artifacts_missing_result_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)

    worktree = tmp_path / "worktrees" / "wor-10"
    worktree.mkdir(parents=True)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-id",
        manifest=manifest,
        worktree_path=worktree,
        process=MagicMock(spec=subprocess.Popen),
    )

    with caplog.at_level(logging.WARNING, logger="app.core.watcher"):
        w._preserve_worker_artifacts(worker)

    assert any("No result artifact" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _rebase_worktree_from_base
# ---------------------------------------------------------------------------


def test_rebase_worktree_from_base_warns_on_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    w = Watcher(linear_client=MagicMock(), repo_root=tmp_path)

    def _raise(*args: Any, **kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, "git", stderr="conflict")

    with (
        patch("subprocess.run", side_effect=_raise),
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        w._rebase_worktree_from_base(tmp_path, "some-epic-branch")

    assert any("Could not rebase" in r.message for r in caplog.records)


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

    mock_start.assert_called_once_with("WOR-99", "fake-linear-id")


def test_dispatch_missing_labels_field_no_crash(tmp_path: Path) -> None:
    mock_linear = MagicMock()
    mock_linear.list_ready_for_local.return_value = [
        {"id": "fake-linear-id", "identifier": "WOR-99", "title": "No labels"}
    ]
    w = Watcher(linear_client=mock_linear, repo_root=tmp_path)

    with patch.object(w, "_start_ticket") as mock_start:
        w._dispatch_next_ticket()

    mock_start.assert_called_once_with("WOR-99", "fake-linear-id")


# ---------------------------------------------------------------------------
# _promote_waiting_tickets — context_snippets cleared on promotion
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _parse_worker_usage
# ---------------------------------------------------------------------------


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "worker_wor-99.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def test_parse_worker_usage_success(tmp_path: Path) -> None:
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
            },
            "context_compactions": 3,
        }
    )
    log = _write_log(tmp_path, ['{"type":"other","x":1}', result_line])
    tokens, compactions = _parse_worker_usage(log)
    assert tokens == 1200
    assert compactions == 3


def test_parse_worker_usage_no_context_compactions(tmp_path: Path) -> None:
    result_line = json.dumps(
        {"type": "result", "usage": {"input_tokens": 500, "output_tokens": 50}}
    )
    log = _write_log(tmp_path, [result_line])
    tokens, compactions = _parse_worker_usage(log)
    assert tokens == 550
    assert compactions is None


def test_parse_worker_usage_missing_log(tmp_path: Path) -> None:
    tokens, compactions = _parse_worker_usage(tmp_path / "no_such_file.log")
    assert tokens is None
    assert compactions is None


def test_parse_worker_usage_no_result_line(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "tool_use", "name": "Bash"}),
            json.dumps({"type": "assistant", "content": "hello"}),
        ],
    )
    tokens, compactions = _parse_worker_usage(log)
    assert tokens is None
    assert compactions is None


def test_parse_worker_usage_malformed_json(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.write_text("not json at all\n{broken\n", encoding="utf-8")
    tokens, compactions = _parse_worker_usage(log)
    assert tokens is None
    assert compactions is None


def test_parse_worker_usage_mixed_valid_invalid_lines(tmp_path: Path) -> None:
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 300, "output_tokens": 100},
            "context_compactions": 1,
        }
    )
    log = tmp_path / "worker.log"
    log.write_text("garbage line\n" + result_line + "\n", encoding="utf-8")
    tokens, compactions = _parse_worker_usage(log)
    assert tokens == 400
    assert compactions == 1


def test_parse_worker_usage_returns_first_result_line(tmp_path: Path) -> None:
    first = json.dumps(
        {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    second = json.dumps(
        {"type": "result", "usage": {"input_tokens": 999, "output_tokens": 999}}
    )
    log = _write_log(tmp_path, [first, second])
    tokens, _ = _parse_worker_usage(log)
    assert tokens == 15


def test_parse_worker_usage_empty_file(tmp_path: Path) -> None:
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    tokens, compactions = _parse_worker_usage(log)
    assert tokens is None
    assert compactions is None


# ---------------------------------------------------------------------------
# _finalize_worker — local_tokens + context_compactions wired from log
# ---------------------------------------------------------------------------


def test_finalize_worker_passes_usage_to_metrics(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-10.log"
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 2000, "output_tokens": 400},
                "context_compactions": 5,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_tokens == 2400
    assert m.context_compactions == 5


def test_finalize_worker_usage_none_when_no_log(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_tokens is None
    assert m.context_compactions is None


# ---------------------------------------------------------------------------
# Per-type concurrency — cloud pool full does not block local dispatch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _fetch_sonar_findings
# ---------------------------------------------------------------------------


def test_fetch_sonar_findings_returns_none_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    monkeypatch.delenv("SONAR_PROJECT_KEY", raising=False)
    w = Watcher(linear_client=MagicMock())
    assert w._fetch_sonar_findings("wor-10-some-branch") is None


def test_fetch_sonar_findings_returns_none_without_project_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.delenv("SONAR_PROJECT_KEY", raising=False)
    w = Watcher(linear_client=MagicMock())
    assert w._fetch_sonar_findings("wor-10-some-branch") is None


def _make_sonar_resp_mock(payload: bytes) -> MagicMock:
    """Return a context-manager mock whose .read() returns payload."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = payload
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_fetch_sonar_findings_returns_severities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-org_my-project")
    api_payload = json.dumps(
        {
            "issues": [
                {"key": "A", "severity": "MAJOR"},
                {"key": "B", "severity": "MINOR"},
                {"key": "C", "severity": "BLOCKER"},
            ]
        }
    ).encode()

    w = Watcher(linear_client=MagicMock())
    mock_resp = _make_sonar_resp_mock(api_payload)
    with patch("urllib.request.urlopen", return_value=mock_resp):
        findings = w._fetch_sonar_findings("wor-10-some-branch")

    assert findings == ["MAJOR", "MINOR", "BLOCKER"]


def test_fetch_sonar_findings_returns_empty_list_when_no_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-project")
    api_payload = json.dumps({"issues": [], "total": 0}).encode()

    w = Watcher(linear_client=MagicMock())
    with patch(
        "urllib.request.urlopen",
        return_value=_make_sonar_resp_mock(api_payload),
    ):
        findings = w._fetch_sonar_findings("wor-10-some-branch")

    assert findings == []


def test_fetch_sonar_findings_returns_none_on_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error

    monkeypatch.setenv("SONAR_TOKEN", "fake-token")
    monkeypatch.setenv("SONAR_PROJECT_KEY", "my-project")
    w = Watcher(linear_client=MagicMock())
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        count = w._fetch_sonar_findings("wor-10-some-branch")
    assert count is None


# ---------------------------------------------------------------------------
# _finalize_worker — sonar_findings_count wired to metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_sonar_count_wired_to_metrics(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(
            w, "_fetch_sonar_findings", return_value=["MAJOR", "MINOR", "MINOR"]
        ),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.sonar_findings_count == 3


def test_finalize_worker_sonar_count_none_when_unavailable(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    w = Watcher(linear_client=MagicMock())
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_fetch_sonar_findings", return_value=None),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.sonar_findings_count is None


# ---------------------------------------------------------------------------
# _finalize_worker — Sonar severity escalation classification
# ---------------------------------------------------------------------------


def _make_finalize_worker_with_empty_result(
    tmp_path: Path,
) -> tuple[Watcher, MagicMock, ActiveWorker]:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({"status": "success"}), encoding="utf-8")

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    return w, linear_mock, worker


def test_finalize_worker_sonar_blocker_escalates(tmp_path: Path) -> None:
    w, linear_mock, worker = _make_finalize_worker_with_empty_result(tmp_path)
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_preserve_worker_artifacts"),
        patch.object(w, "_fetch_sonar_findings", return_value=["BLOCKER"]),
        patch.object(w, "_create_pr") as mock_create_pr,
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"
    assert m.sonar_findings_count == 1


def test_finalize_worker_sonar_major_advisory_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w, _linear_mock, worker = _make_finalize_worker_with_empty_result(tmp_path)
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_preserve_worker_artifacts"),
        patch.object(w, "_fetch_sonar_findings", return_value=["MAJOR"]),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
        caplog.at_level(logging.WARNING, logger="app.core.watcher"),
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is False
    assert m.outcome == "success"
    assert m.sonar_findings_count == 1
    assert any("MAJOR" in msg and "fix_locally" in msg for msg in caplog.messages)


def test_finalize_worker_sonar_none_no_escalation(tmp_path: Path) -> None:
    w, _linear_mock, worker = _make_finalize_worker_with_empty_result(tmp_path)
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_preserve_worker_artifacts"),
        patch.object(w, "_fetch_sonar_findings", return_value=None),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is False
    assert m.outcome == "success"
    assert m.sonar_findings_count is None


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

    # max_cloud_workers=1 so one occupant fills the cloud pool; local cap=1
    watcher = Watcher(
        linear_client=mock_linear,
        max_local_workers=1,
        max_cloud_workers=1,
    )

    # Pre-fill the cloud pool
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
        patch.object(watcher, "_create_worktree", return_value=tmp_path),
        patch.object(watcher, "_copy_manifest_to_worktree"),
        patch.object(watcher, "_write_worker_pytest_config"),
        patch.object(watcher, "_launch_worker", return_value=fake_local_process),
    ):
        watcher._start_ticket("WOR-10", "fake-local-id")

    # Local ticket dispatched despite cloud pool being full
    assert len(watcher._local_active) == 1
    assert watcher._local_active[0].ticket_id == "WOR-10"
    # Cloud pool unchanged
    assert len(watcher._cloud_active) == 1
    assert watcher._cloud_active[0].ticket_id == "WOR-99"


# ---------------------------------------------------------------------------
# _finalize_worker — EscalationPolicy flag routing
# ---------------------------------------------------------------------------


def _make_finalize_worker_for_policy(
    tmp_path: Path,
    flags: dict[str, bool],
) -> tuple[Watcher, MagicMock, ActiveWorker]:
    """Return (watcher, linear_mock, worker) with result.json pre-written."""
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    result_path = tmp_path / ".claude" / "artifacts" / "wor_10" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({"status": "success", **flags}), encoding="utf-8")

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    return w, linear_mock, worker


def test_finalize_worker_scope_drift_escalates(tmp_path: Path) -> None:
    w, linear_mock, worker = _make_finalize_worker_for_policy(
        tmp_path, {"scope_drift": True}
    )
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_preserve_worker_artifacts"),
        patch.object(w, "_create_pr") as mock_create_pr,
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "scope_drift" in comment_body
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"


def test_finalize_worker_forbidden_path_touched_escalates(tmp_path: Path) -> None:
    w, linear_mock, worker = _make_finalize_worker_for_policy(
        tmp_path, {"forbidden_path_touched": True}
    )
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_preserve_worker_artifacts"),
        patch.object(w, "_create_pr") as mock_create_pr,
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "In Progress")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "forbidden_path_touched" in comment_body
    m = metrics_mock.record.call_args[0][0]
    assert m.escalated_to_cloud is True
    assert m.outcome == "escalated"


def test_finalize_worker_no_flags_proceeds_normally(tmp_path: Path) -> None:
    w, _linear_mock, worker = _make_finalize_worker_for_policy(tmp_path, {})
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_preserve_worker_artifacts"),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False


def test_finalize_worker_missing_result_json_proceeds_normally(tmp_path: Path) -> None:
    manifest = _make_manifest(ticket_id="WOR-10", worker_branch="wor-10-test-ticket")
    linear_mock = MagicMock()
    w = Watcher(linear_client=linear_mock, repo_root=tmp_path)

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch.object(w, "_run_checks", return_value=True),
        patch.object(w, "_preserve_worker_artifacts"),
        patch.object(w, "_create_pr", return_value="https://github.com/example/pr/1"),
        patch.object(w, "_cleanup_worktree"),
        patch.object(w, "_metrics") as metrics_mock,
    ):
        w._finalize_worker(worker, returncode=0, wall_time=1.0)

    m = metrics_mock.record.call_args[0][0]
    assert m.outcome == "success"
    assert m.escalated_to_cloud is False
