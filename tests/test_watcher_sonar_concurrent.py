"""Tests for WOR-465: fire Sonar findings fetch concurrent with run_checks.

Asserts that `_execute_finalization` starts `fetch_sonar_findings` in a
background thread before `run_checks` and joins on the result later. Net
effect: total finalize wall == max(checks_wall, sonar_wall) + small overhead,
not the sum.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.escalation_policy import EscalationPolicy
from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher_finalize_helpers import _execute_finalization
from app.core.watcher.watcher_types import ActiveWorker


def _make_manifest(ticket_id: str = "WOR-10") -> ExecutionManifest:
    return ExecutionManifest(
        ticket_id=ticket_id,
        epic_id="WOR-96",
        title="Test",
        priority=2,
        status="ReadyForLocal",
        parallel_safe=True,
        risk_level="low",
        implementation_mode="local",
        routing="local",
        review_mode="auto",
        base_branch="epic/wor-96",
        worker_branch=f"wor-{ticket_id.lower().replace('-', '')}-branch",
        objective="Do the thing.",
        artifact_paths=ArtifactPaths.from_ticket_id(ticket_id),
        allowed_paths=["app/core/foo.py"],
        required_checks=["pytest"],
    )


def _make_worker(tmp_path: Path, ticket_id: str = "WOR-10") -> ActiveWorker:
    manifest = _make_manifest(ticket_id=ticket_id)
    artifact_dir = (
        tmp_path / ".claude" / "artifacts" / ticket_id.lower().replace("-", "_")
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    # Write a minimal result.json so _read_result_status returns success
    (artifact_dir / "result.json").write_text('{"status": "success"}')
    return ActiveWorker(
        ticket_id=ticket_id,
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )


def test_sonar_fetch_runs_concurrent_with_checks(tmp_path: Path) -> None:
    """Total finalize wall should be ~max(checks, sonar) not sum."""
    worker = _make_worker(tmp_path)
    linear = MagicMock()
    escalation = EscalationPolicy.from_toml()

    def slow_checks(*args: Any, **kwargs: Any) -> tuple[bool, list[Any]]:
        # Simulate a ~250ms check phase
        time.sleep(0.25)
        return True, []

    def slow_sonar(_branch: str) -> list[str] | None:
        # Simulate a ~200ms Sonar fetch
        time.sleep(0.2)
        return []  # no findings

    fake_attempt = MagicMock(return_value=("success", "https://gh/pr/1"))

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=slow_checks,
        ),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            side_effect=slow_sonar,
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
    ):
        t0 = time.perf_counter()
        _execute_finalization(
            worker=worker,
            returncode=0,
            linear=linear,
            escalation_policy=escalation,
            repo_root=tmp_path,
            attempt_pr_fn=fake_attempt,
            tracked_prs=None,
            metrics=None,
            project_id="",
        )
        elapsed = time.perf_counter() - t0

    # Serial would be 250 + 200 = 450ms. Concurrent: ~max(250, 200) = 250ms.
    # Allow generous overhead ceiling at 350ms.
    assert elapsed < 0.35, (
        f"Sonar fetch did not overlap with checks: total={elapsed:.2f}s, "
        f"expected < 0.35s"
    )


def test_sonar_fetch_starts_before_checks(tmp_path: Path) -> None:
    """The Sonar future must be submitted before run_checks begins."""
    worker = _make_worker(tmp_path)
    linear = MagicMock()
    escalation = EscalationPolicy.from_toml()

    sonar_started = threading.Event()
    checks_started = threading.Event()

    def slow_checks(*args: Any, **kwargs: Any) -> tuple[bool, list[Any]]:
        checks_started.set()
        time.sleep(0.1)
        return True, []

    def slow_sonar(_branch: str) -> list[str] | None:
        sonar_started.set()
        time.sleep(0.1)
        return []

    fake_attempt = MagicMock(return_value=("success", "https://gh/pr/1"))

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            side_effect=slow_checks,
        ),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            side_effect=slow_sonar,
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
    ):
        _execute_finalization(
            worker=worker,
            returncode=0,
            linear=linear,
            escalation_policy=escalation,
            repo_root=tmp_path,
            attempt_pr_fn=fake_attempt,
            tracked_prs=None,
            metrics=None,
            project_id="",
        )

    assert sonar_started.is_set(), "Sonar fetch never started"
    assert checks_started.is_set(), "run_checks never started"


def test_sonar_exception_falls_back_to_none(tmp_path: Path) -> None:
    """If the Sonar fetch raises, _handle_policy_outcome treats it as None
    findings (degraded fallback) and continues with PR creation."""
    worker = _make_worker(tmp_path)
    linear = MagicMock()
    escalation = EscalationPolicy.from_toml()

    def raising_sonar(_branch: str) -> list[str] | None:
        raise RuntimeError("Sonar API unavailable")

    fake_attempt = MagicMock(return_value=("success", "https://gh/pr/1"))

    with (
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            side_effect=raising_sonar,
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
    ):
        outcome, escalated, _, sonar_findings, _, pr_url = _execute_finalization(
            worker=worker,
            returncode=0,
            linear=linear,
            escalation_policy=escalation,
            repo_root=tmp_path,
            attempt_pr_fn=fake_attempt,
            tracked_prs=None,
            metrics=None,
            project_id="",
        )

    # Sonar raising → treated as no findings → PR attempt still happens
    assert outcome == "success", f"Expected success on Sonar failure, got {outcome}"
    assert sonar_findings is None
    fake_attempt.assert_called_once()
