"""Tests for the watcher/orchestrator pure logic functions.

Integration tests (actually launching subprocesses, Linear API) are out of scope;
this file covers the unit-testable, I/O-free helpers.
"""

from __future__ import annotations

import json
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


def test_cloud_cmd_has_no_model_flag() -> None:
    cmd = build_worker_cmd("WOR-10", "cloud")
    assert "--model" not in cmd
    assert "/implement-ticket WOR-10" in " ".join(cmd)


def test_local_cmd_includes_model_flag() -> None:
    cmd = build_worker_cmd("WOR-10", "local")
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "qwen3-coder:30b"


def test_cmd_includes_dangerously_skip_permissions() -> None:
    for mode in ("cloud", "local"):
        cmd = build_worker_cmd("WOR-10", mode)
        assert "--dangerously-skip-permissions" in cmd


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
    worktree_dir = tmp_path / ".claude/worktrees/wor-99-old-ticket"
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
        # Success path calls set_state(merged_to_epic) — must not propagate
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
        # set_state raises — worker must still be launched and added to _active
        w._start_ticket("WOR-10", "fake-linear-id")

    assert len(w._active) == 1
    assert w._active[0].ticket_id == "WOR-10"


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
