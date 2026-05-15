"""WOR-457: last_failure.json written for ANY finalize failure stage.

Before WOR-457 the diagnostic artifact was written only by the run_checks
path. Failures at rebase / push / pr_create / validation gates left the
operator with a Linear "Blocked" and no last_failure.json (WOR-441).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.escalation_policy import EscalationPolicy
from app.core.manifest import ArtifactPaths
from app.core.watcher.watcher_finalize import finalize_worker
from app.core.watcher.watcher_finalize_helpers import (
    _classify_stage,
    _record_failure_artifact,
)
from app.core.watcher.watcher_types import ActiveWorker
from tests.conftest import make_manifest


def _artifact_dir(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "artifacts" / "wor_457"


def _make_worker(tmp_path: Path) -> ActiveWorker:
    manifest = make_manifest(
        ticket_id="WOR-457",
        worker_branch="wor-457-test",
        required_checks=["ruff check .", "mypy app/", "pytest", "lint-imports"],
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-457"),
    )
    return ActiveWorker(
        ticket_id="WOR-457",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )


# ---------------------------------------------------------------------------
# _classify_stage
# ---------------------------------------------------------------------------


def test_classify_stage_rebase() -> None:
    exc = subprocess.CalledProcessError(1, ["git", "rebase", "main"])
    assert _classify_stage(exc) == "rebase"


def test_classify_stage_push() -> None:
    exc = subprocess.CalledProcessError(1, ["git", "push", "--force-with-lease"])
    assert _classify_stage(exc) == "push"


def test_classify_stage_pr_create() -> None:
    exc = subprocess.CalledProcessError(1, ["gh", "pr", "create", "--base", "main"])
    assert _classify_stage(exc) == "pr_create"


def test_classify_stage_pr_merge() -> None:
    exc = subprocess.CalledProcessError(1, ["gh", "pr", "merge", "--auto"])
    assert _classify_stage(exc) == "pr_merge"


def test_classify_stage_run_checks() -> None:
    exc = subprocess.CalledProcessError(1, ["pytest", "-q"])
    assert _classify_stage(exc) == "run_checks"


def test_classify_stage_parse() -> None:
    exc = json.JSONDecodeError("bad", "doc", 0)
    assert _classify_stage(exc) == "parse"


def test_classify_stage_other() -> None:
    assert _classify_stage(RuntimeError("something unexpected")) == "other"


# ---------------------------------------------------------------------------
# _record_failure_artifact
# ---------------------------------------------------------------------------


def test_record_failure_artifact_writes_stage_and_check_none(
    tmp_path: Path,
) -> None:
    d = tmp_path / "art"
    _record_failure_artifact(d, "pr_create", exception=RuntimeError("boom"))
    data = json.loads((d / "last_failure.json").read_text(encoding="utf-8"))
    assert data["stage"] == "pr_create"
    assert data["check"] is None
    assert "failed_at" in data
    assert "RuntimeError: boom" in data["exception"]


def test_record_failure_artifact_merges_preserving_check_and_stdout(
    tmp_path: Path,
) -> None:
    """WOR-66 round-trip: run_checks wrote {check, stdout}; adding stage
    keeps both."""
    d = tmp_path / "art"
    d.mkdir()
    (d / "last_failure.json").write_text(
        json.dumps({"check": "pytest", "stdout": "FAILED test_x"}),
        encoding="utf-8",
    )
    _record_failure_artifact(d, "run_checks", check="pytest")
    data = json.loads((d / "last_failure.json").read_text(encoding="utf-8"))
    assert data["stage"] == "run_checks"
    assert data["check"] == "pytest"
    assert data["stdout"] == "FAILED test_x"


def test_record_failure_artifact_keep_existing_stage_no_downgrade(
    tmp_path: Path,
) -> None:
    """A generic 'other' must not overwrite a precise prior stage."""
    d = tmp_path / "art"
    d.mkdir()
    (d / "last_failure.json").write_text(
        json.dumps({"stage": "pr_create"}), encoding="utf-8"
    )
    _record_failure_artifact(d, "other", keep_existing_stage=True)
    data = json.loads((d / "last_failure.json").read_text(encoding="utf-8"))
    assert data["stage"] == "pr_create"


# ---------------------------------------------------------------------------
# Integration: validation-gate failures produce last_failure.json
# ---------------------------------------------------------------------------


def test_finalize_allowed_paths_violation_writes_stage(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    with (
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=["ALLOWED app/ui/widget.py"],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
    ):
        outcome = finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=MagicMock(),
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    assert outcome == "failure"
    data = json.loads(
        (_artifact_dir(tmp_path) / "last_failure.json").read_text(encoding="utf-8")
    )
    assert data["stage"] == "validate_allowed_paths"
    assert "app/ui/widget.py" in data["stderr"]


def test_finalize_checks_passed_violation_writes_stage(tmp_path: Path) -> None:
    worker = _make_worker(tmp_path)
    result_path = _artifact_dir(tmp_path) / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"status": "success", "checks_passed": ["ruff", "bandit"]}),
        encoding="utf-8",
    )
    with (
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
    ):
        outcome = finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=MagicMock(),
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    assert outcome == "failure"
    data = json.loads(
        (_artifact_dir(tmp_path) / "last_failure.json").read_text(encoding="utf-8")
    )
    assert data["stage"] == "validate_checks_passed"
    assert "missing required_checks" in data["stderr"]


# ---------------------------------------------------------------------------
# wip_status round-trip: commit_wip_state → last_failure.json
# ---------------------------------------------------------------------------


def test_finalize_worker_writes_clean_wip_status(tmp_path: Path) -> None:
    """clean tree → last_failure.json contains wip_status=clean."""
    worker = _make_worker(tmp_path)
    with (
        patch("app.core.watcher.watcher_finalize.commit_wip_state") as mock_wip,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
    ):
        from app.core.watcher.watcher_worktrees import WipPreservationResult

        mock_wip.return_value = WipPreservationResult(
            status="clean", sha=None, backup_path=None, error=None
        )
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=MagicMock(),
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    data = json.loads(
        (_artifact_dir(tmp_path) / "last_failure.json").read_text(encoding="utf-8")
    )
    assert data["wip_status"] == "clean"


def test_finalize_worker_writes_pushed_wip_status(tmp_path: Path) -> None:
    """Successful push → wip_status=pushed with sha."""
    worker = _make_worker(tmp_path)
    with (
        patch("app.core.watcher.watcher_finalize.commit_wip_state") as mock_wip,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
    ):
        from app.core.watcher.watcher_worktrees import WipPreservationResult

        mock_wip.return_value = WipPreservationResult(
            status="pushed", sha="abc1234", backup_path=None, error=None
        )
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=MagicMock(),
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    data = json.loads(
        (_artifact_dir(tmp_path) / "last_failure.json").read_text(encoding="utf-8")
    )
    assert data["wip_status"] == "pushed"
    assert data["wip_commit_sha"] == "abc1234"


def test_finalize_worker_writes_backup_wip_status(tmp_path: Path) -> None:
    """Commit failed, backup succeeded → wip_status=backup with path."""
    worker = _make_worker(tmp_path)
    with (
        patch("app.core.watcher.watcher_finalize.commit_wip_state") as mock_wip,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
    ):
        from app.core.watcher.watcher_worktrees import WipPreservationResult

        backup_path = tmp_path / ".claude" / "artifacts" / "wor_457" / "wip"
        mock_wip.return_value = WipPreservationResult(
            status="backup", sha=None, backup_path=backup_path, error="push failed"
        )
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=MagicMock(),
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    data = json.loads(
        (_artifact_dir(tmp_path) / "last_failure.json").read_text(encoding="utf-8")
    )
    assert data["wip_status"] == "backup"
    assert data["wip_backup_path"] == str(backup_path)


def test_finalize_worker_writes_failed_wip_status(tmp_path: Path) -> None:
    """commit + push + backup all failed → wip_status=failed."""
    worker = _make_worker(tmp_path)
    with (
        patch("app.core.watcher.watcher_finalize.commit_wip_state") as mock_wip,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
        patch("app.core.watcher.watcher_finalize.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
    ):
        from app.core.watcher.watcher_worktrees import WipPreservationResult

        mock_wip.return_value = WipPreservationResult(
            status="failed", sha=None, backup_path=None, error="git error"
        )
        finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=MagicMock(),
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    data = json.loads(
        (_artifact_dir(tmp_path) / "last_failure.json").read_text(encoding="utf-8")
    )
    assert data["wip_status"] == "failed"
