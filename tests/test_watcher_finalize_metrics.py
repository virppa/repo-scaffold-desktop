"""Metrics-population tests for finalize_worker.

Extracted from test_watcher_finalize.py for the file-size gate (WOR-282).
Covers WOR-261 (check_failures + sonar_findings_count), WOR-263 (local_model
for local runs), and the cloud-pricing helpers (_resolve_cloud_model,
_estimate_cloud_cost).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.escalation_policy import EscalationPolicy
from app.core.watcher.watcher_finalize import finalize_worker
from app.core.watcher.watcher_types import ActiveWorker
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEFAULT_PROJECT = "repo-scaffold-desktop"


def _call_finalize(
    worker: ActiveWorker,
    *,
    returncode: int = 0,
    wall_time: float = 1.0,
    linear: object | None = None,
    metrics: object | None = None,
    repo_root: Path | None = None,
    mode: str = "default",
) -> None:
    finalize_worker(
        worker,
        returncode=returncode,
        wall_time=wall_time,
        linear=linear or MagicMock(),
        metrics=metrics or MagicMock(),
        escalation_policy=EscalationPolicy.from_toml(),
        repo_root=repo_root or Path("."),
        mode=mode,
        project_id=_DEFAULT_PROJECT,
    )


# ---------------------------------------------------------------------------
# WOR-261 — check_failures_json and sonar_findings_count wired to metrics
# ---------------------------------------------------------------------------


def test_finalize_worker_check_failures_populated_on_check_failure(
    tmp_path: Path,
) -> None:
    """A failing check produces check_failures with the correct check name."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        required_checks=["ruff check .", "mypy app/", "pytest"],
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(
                False,
                [{"check": "mypy app/", "exit_code": 1}],
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.check_failures is not None
    assert len(m.check_failures) == 1
    assert m.check_failures[0]["check"] == "mypy app/"
    assert m.check_failures[0]["exit_code"] == 1


def test_finalize_worker_check_failures_empty_on_success(
    tmp_path: Path,
) -> None:
    """All checks pass → check_failures is None (serialises to '[]')."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.check_failures is None
    # WOR-284 — git-diff fields are zero when worktree has no base branch
    assert m.lines_changed == 0
    assert m.files_changed == 0


def test_finalize_worker_sonar_count_zero_on_empty_findings(
    tmp_path: Path,
) -> None:
    """Success-path finalization with fetch_sonar_findings returning []
    produces sonar_findings_count=0 (not null)."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=[],
        ),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.sonar_findings_count == 0


def test_finalize_worker_failed_check_in_run_log_on_failure(
    tmp_path: Path,
) -> None:
    """TicketRunLog.failed_check is set to the first failed check name."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
        required_checks=["ruff check .", "mypy app/"],
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(
                False,
                [
                    {"check": "ruff check .", "exit_code": 1},
                    {"check": "mypy app/", "exit_code": 2},
                ],
            ),
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    run_call = metrics_mock.record_run.call_args[0][0]
    assert run_call.failed_check == "ruff check ."


# ---------------------------------------------------------------------------
# WOR-260 — cloud_model, cloud_tokens, cloud_cost_estimate for cloud runs
# ---------------------------------------------------------------------------


def test_finalize_worker_cloud_metrics_populated_for_cloud_run(
    tmp_path: Path,
) -> None:
    """Cloud finalization records cloud_model, cloud_tokens, cloud_cost_estimate."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    metrics_mock = MagicMock()

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-10.log"
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 20000, "output_tokens": 500},
                "context_compactions": 1,
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, mode="cloud", metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.cloud_used is True
    assert m.local_used is False
    assert m.cloud_model == "claude-opus-4-7"  # default
    assert m.cloud_tokens == 20500  # 20000 + 500
    # claude-opus-4-7: input $15/1M, output $75/1M
    expected_cost = (20000 / 1_000_000) * 15.0 + (500 / 1_000_000) * 75.0
    assert m.cloud_cost_estimate == pytest.approx(expected_cost)
    # Local-specific fields must be None for cloud runs
    assert m.local_input_tokens is None
    assert m.local_output_tokens is None
    assert m.local_tokens is None
    assert m.local_model is None
    assert m.output_tokens_per_wall_second is None


def test_finalize_worker_cloud_model_from_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTHROPIC_MODEL env-var overrides the default cloud model."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    metrics_mock = MagicMock()

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-10.log"
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, mode="cloud", metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.cloud_model == "claude-sonnet-4-6"
    # claude-sonnet-4-6: input $3/1M, output $15/1M
    expected_cost = (1000 / 1_000_000) * 3.0 + (100 / 1_000_000) * 15.0
    assert m.cloud_cost_estimate == pytest.approx(expected_cost)


def test_finalize_worker_cloud_no_token_log(
    tmp_path: Path,
) -> None:
    """Without a log file, cloud tokens/cost are None for cloud runs."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    metrics_mock = MagicMock()

    worker = ActiveWorker(
        ticket_id="WOR-10",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, mode="cloud", metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.cloud_model is None
    assert m.cloud_tokens is None
    assert m.cloud_cost_estimate is None
    assert m.cloud_used is True
    assert m.local_used is False


def test_finalize_worker_local_run_keeps_local_fields(
    tmp_path: Path,
) -> None:
    """Local runs still populate local_* fields; cloud_* stay None."""
    manifest = make_manifest(
        ticket_id="WOR-10",
        worker_branch="wor-10-test-ticket",
    )
    metrics_mock = MagicMock()

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-10.log"
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 15000, "output_tokens": 600},
                "context_compactions": 2,
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
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, wall_time=10.0, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_used is True
    assert m.cloud_used is False
    assert m.local_input_tokens == 15000
    assert m.local_output_tokens == 600
    assert m.local_tokens == 15600
    assert m.output_tokens_per_wall_second == pytest.approx(60.0)
    # Cloud fields must be None for local runs
    assert m.cloud_model is None
    assert m.cloud_tokens is None
    assert m.cloud_cost_estimate is None


# ---------------------------------------------------------------------------
# WOR-263 — local_model always populated for local runs
# ---------------------------------------------------------------------------


def test_finalize_worker_local_model_non_null_for_local_run(
    tmp_path: Path,
) -> None:
    """local_model is non-null for local runs and matches _LOCAL_MODEL constant."""
    manifest = make_manifest(
        ticket_id="WOR-263",
        worker_branch="wor-263-test-ticket",
    )
    metrics_mock = MagicMock()

    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "worker_wor-263.log"
    log_file.write_text(
        json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 15000, "output_tokens": 600},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    worker = ActiveWorker(
        ticket_id="WOR-263",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, wall_time=10.0, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    from app.core.watcher.watcher_types import _LOCAL_MODEL

    assert m.local_model == _LOCAL_MODEL
    assert m.local_model is not None
    assert m.local_used is True


def test_finalize_worker_local_model_is_none_for_cloud_run(
    tmp_path: Path,
) -> None:
    """local_model is None for cloud runs."""
    manifest = make_manifest(
        ticket_id="WOR-263",
        worker_branch="wor-263-test-ticket",
    )
    metrics_mock = MagicMock()

    worker = ActiveWorker(
        ticket_id="WOR-263",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, mode="cloud", metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.local_model is None


# ---------------------------------------------------------------------------
# WOR-260 — _resolve_cloud_model and _estimate_cloud_cost unit tests
# ---------------------------------------------------------------------------


def test_resolve_cloud_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ANTHROPIC_MODEL env-var, returns the default model."""
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    from app.core.watcher.watcher_finalize import _resolve_cloud_model

    assert _resolve_cloud_model() == "claude-opus-4-7"


def test_resolve_cloud_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ANTHROPIC_MODEL is set, that value is returned."""
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    from app.core.watcher.watcher_finalize import _resolve_cloud_model

    assert _resolve_cloud_model() == "claude-haiku-4-5"


def test_estimate_cloud_cost_opus() -> None:
    """claude-opus-4-7: input $15/1M, output $75/1M."""
    from app.core.watcher.watcher_finalize import _estimate_cloud_cost

    cost = _estimate_cloud_cost(100000, 100000, "claude-opus-4-7")
    expected = (100000 / 1_000_000) * 15.0 + (100000 / 1_000_000) * 75.0
    assert cost == pytest.approx(expected)


def test_estimate_cloud_cost_sonnet() -> None:
    """claude-sonnet-4-6: input $3/1M, output $15/1M."""
    from app.core.watcher.watcher_finalize import _estimate_cloud_cost

    cost = _estimate_cloud_cost(200000, 50000, "claude-sonnet-4-6")
    expected = (200000 / 1_000_000) * 3.0 + (50000 / 1_000_000) * 15.0
    assert cost == pytest.approx(expected)


def test_estimate_cloud_cost_haiku() -> None:
    """claude-haiku-4-5: input $0.80/1M, output $4/1M."""
    from app.core.watcher.watcher_finalize import _estimate_cloud_cost

    cost = _estimate_cloud_cost(500000, 100000, "claude-haiku-4-5")
    expected = (500000 / 1_000_000) * 0.80 + (100000 / 1_000_000) * 4.0
    assert cost == pytest.approx(expected)


def test_estimate_cloud_cost_unknown_model_returns_zero() -> None:
    """Unknown model returns 0.0."""
    from app.core.watcher.watcher_finalize import _estimate_cloud_cost

    assert _estimate_cloud_cost(1000, 1000, "unknown-model") == 0.0


def test_estimate_cloud_cost_none_tokens_returns_zero() -> None:
    """None input or output tokens returns 0.0."""
    from app.core.watcher.watcher_finalize import _estimate_cloud_cost

    assert _estimate_cloud_cost(None, 1000, "claude-opus-4-7") == 0.0
    assert _estimate_cloud_cost(1000, None, "claude-opus-4-7") == 0.0


# ---------------------------------------------------------------------------
# WOR-354 — three-dot diff against merge-base, invariant under base drift
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run a git command in repo and return stdout (utf-8, stripped)."""
    return subprocess.run(  # nosec B603 B607
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _setup_repo_with_drift(
    tmp_path: Path,
    *,
    worker_loc: int,
    drift_loc: int,
) -> Path:
    """Build a real git repo simulating a worker branch off an epic branch
    that subsequently drifted by ``drift_loc`` insertions on the base.

    Layout:
      main (initial)
        └── epic (base_branch)
              ├── worker_HEAD (adds worker_loc lines in worker.py)
              └── (epic advances by drift_loc lines in sibling.py)

    The returned worktree directory is checked out at worker_HEAD.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")

    _git(repo, "checkout", "-q", "-b", "epic")
    (repo / "epic_seed.py").write_text("# epic seed\n")
    _git(repo, "add", "epic_seed.py")
    _git(repo, "commit", "-q", "-m", "epic base")

    _git(repo, "checkout", "-q", "-b", "worker")
    if worker_loc > 0:
        worker_body = "\n".join("x" for _ in range(worker_loc)) + "\n"
        (repo / "worker.py").write_text(worker_body)
        _git(repo, "add", "worker.py")
        _git(repo, "commit", "-q", "-m", "worker change")

    _git(repo, "checkout", "-q", "epic")
    sibling_body = "\n".join("y" for _ in range(drift_loc)) + "\n"
    (repo / "sibling.py").write_text(sibling_body)
    _git(repo, "add", "sibling.py")
    _git(repo, "commit", "-q", "-m", "sibling merged into epic during worker run")

    _git(repo, "checkout", "-q", "worker")
    return repo


def _finalize_with_real_diff(repo: Path, ticket_id: str = "WOR-354") -> object:
    """Run finalize_worker against a real git repo with all other steps mocked."""
    manifest = make_manifest(
        ticket_id=ticket_id,
        worker_branch="worker",
        base_branch="epic",
    )
    metrics_mock = MagicMock()
    worker = ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=repo,
        process=MagicMock(spec=subprocess.Popen),
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=[],
        ),
    ):
        _call_finalize(worker, metrics=metrics_mock)
    return metrics_mock.record.call_args[0][0]


def test_lines_changed_excludes_base_drift(tmp_path: Path) -> None:
    """Three-dot diff: worker contributes 5 LOC; base advances 100 LOC during
    the run; lines_changed must reflect only the worker's 5 LOC."""
    repo = _setup_repo_with_drift(tmp_path, worker_loc=5, drift_loc=100)

    m = _finalize_with_real_diff(repo)

    # Three-dot semantics: only the worker's commits relative to the merge-base.
    assert m.lines_changed == 5
    assert m.files_changed == 1


def test_lines_changed_zero_when_worker_no_op_under_drift(tmp_path: Path) -> None:
    """Worker makes zero commits but base advances 100 LOC: lines_changed=0
    (regression for the WOR-318/319/320/321 byte-identical-footprint pattern)."""
    repo = _setup_repo_with_drift(tmp_path, worker_loc=0, drift_loc=100)

    m = _finalize_with_real_diff(repo)

    assert m.lines_changed == 0
    assert m.files_changed == 0


# ---------------------------------------------------------------------------
# WOR-274 — hook-trust violations
# ---------------------------------------------------------------------------


def test_finalize_worker_hook_trust_violation_warning(
    tmp_path: Path, caplog: "pytest.LogCaptureFixture"
) -> None:
    """When the worker log contains >1 manual check invocations, finalize_worker
    emits a WARNING and the count lands in TicketMetrics.hook_trust_violations."""
    import logging

    caplog.set_level(logging.WARNING)

    manifest = make_manifest(
        ticket_id="WOR-274",
        worker_branch="wor-274-test-ticket",
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    # Write a log with 3 manual check invocations (ruff + mypy + pytest)
    # finalize_worker looks at worker.worktree_path / ".claude/worker_<id>.log"
    log_dir = tmp_path
    log_dir = tmp_path
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / ".claude" / "worker_wor-274.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "ruff check ."},
                            },
                        ]
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "mypy app/"},
                            },
                        ]
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "pytest tests/"},
                            },
                        ]
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 20000, "output_tokens": 500},
                }
            )
            + "\n"
        ),
        encoding="utf-8",
    )

    worker = ActiveWorker(
        ticket_id="WOR-274",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=log_dir,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.hook_trust_violations == 3

    assert any("hook-trust violation" in rec.message for rec in caplog.records)


def test_finalize_worker_no_warning_when_violations_leq_one(
    tmp_path: Path, caplog: "pytest.LogCaptureFixture"
) -> None:
    """A single manual check invocation does NOT trigger the WARNING.
    Threshold is >1.
    """
    import logging

    caplog.set_level(logging.WARNING)

    manifest = make_manifest(
        ticket_id="WOR-274-single",
        worker_branch="wor-274-single-test",
    )
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    log_dir = tmp_path
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / ".claude" / "worker_wor-274-single.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ruff check ."},
                        },
                    ]
                },
            }
        )
        + "\n"
        + json.dumps(
            {"type": "result", "usage": {"input_tokens": 20000, "output_tokens": 500}}
        )
        + "\n",
        encoding="utf-8",
    )

    worker = ActiveWorker(
        ticket_id="WOR-274-single",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=log_dir,
        process=MagicMock(spec=subprocess.Popen),
    )

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ),
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        _call_finalize(worker, linear=linear_mock, metrics=metrics_mock)

    m = metrics_mock.record.call_args[0][0]
    assert m.hook_trust_violations == 1
    # No WARNING should be emitted for count == 1
    assert not any("hook-trust violation" in rec.message for rec in caplog.records)
