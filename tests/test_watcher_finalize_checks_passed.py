"""WOR-456: watcher cross-checks result.json.checks_passed vs required_checks.

A worker that writes ``status: success`` with ``checks_passed`` populated from
pre-commit hook names (``ruff``, ``ruff-format`` …) instead of the manifest's
``required_checks`` command strings has not run the contract checks. The
watcher must reject this at finalize time (Blocked) rather than open a PR.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.escalation_policy import EscalationPolicy
from app.core.manifest import ArtifactPaths
from app.core.watcher.watcher_finalize import finalize_worker
from app.core.watcher.watcher_finalize_helpers import _validate_checks_passed
from app.core.watcher.watcher_types import ActiveWorker
from tests.conftest import make_manifest

_REQUIRED = ["ruff check .", "mypy app/", "pytest", "lint-imports"]
_PRECOMMIT_NAMES = [
    "ruff",
    "ruff-format",
    "bandit",
    "trailing-whitespace",
    "end-of-file-fixer",
]


def _write_result(tmp_path: Path, payload: dict[str, object]) -> None:
    result_path = tmp_path / ".claude" / "artifacts" / "wor_456" / "result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload), encoding="utf-8")


def _make_worker(tmp_path: Path) -> ActiveWorker:
    manifest = make_manifest(
        ticket_id="WOR-456",
        worker_branch="wor-456-test",
        required_checks=_REQUIRED,
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-456"),
    )
    return ActiveWorker(
        ticket_id="WOR-456",
        linear_id="fake-linear-id",
        manifest=manifest,
        worktree_path=tmp_path,
        process=MagicMock(spec=subprocess.Popen),
    )


# ---------------------------------------------------------------------------
# Unit: _validate_checks_passed
# ---------------------------------------------------------------------------


def test_validate_checks_passed_precommit_names_all_missing(tmp_path: Path) -> None:
    """Pre-commit hook names → every required_check reported missing."""
    worker = _make_worker(tmp_path)
    _write_result(tmp_path, {"status": "success", "checks_passed": _PRECOMMIT_NAMES})
    missing = _validate_checks_passed(worker.manifest, tmp_path)
    assert missing == _REQUIRED


def test_validate_checks_passed_exact_names_clean(tmp_path: Path) -> None:
    """Exact required_checks strings → no missing checks."""
    worker = _make_worker(tmp_path)
    _write_result(tmp_path, {"status": "success", "checks_passed": _REQUIRED})
    assert _validate_checks_passed(worker.manifest, tmp_path) == []


def test_validate_checks_passed_partial_reports_only_gap(tmp_path: Path) -> None:
    """Worker ran 3 of 4 → only the unrun check is flagged."""
    worker = _make_worker(tmp_path)
    _write_result(
        tmp_path,
        {"status": "success", "checks_passed": ["ruff check .", "mypy app/", "pytest"]},
    )
    assert _validate_checks_passed(worker.manifest, tmp_path) == ["lint-imports"]


def test_validate_checks_passed_empty_required_is_clean(tmp_path: Path) -> None:
    """Empty required_checks → nothing to enforce."""
    manifest = make_manifest(
        ticket_id="WOR-456",
        worker_branch="wor-456-test",
        required_checks=[],
        artifact_paths=ArtifactPaths.from_ticket_id("WOR-456"),
    )
    _write_result(tmp_path, {"status": "success", "checks_passed": []})
    assert _validate_checks_passed(manifest, tmp_path) == []


def test_validate_checks_passed_missing_result_is_clean(tmp_path: Path) -> None:
    """Unreadable result.json is the returncode path's job, not a violation."""
    worker = _make_worker(tmp_path)
    assert _validate_checks_passed(worker.manifest, tmp_path) == []


# ---------------------------------------------------------------------------
# Integration: finalize_worker gate
# ---------------------------------------------------------------------------


def test_finalize_rejects_precommit_names_as_contract_violation(
    tmp_path: Path,
) -> None:
    """result.json with pre-commit hook names → Blocked + comment, no PR."""
    worker = _make_worker(tmp_path)
    _write_result(tmp_path, {"status": "success", "checks_passed": _PRECOMMIT_NAMES})
    linear_mock = MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
        patch("app.core.watcher.watcher_finalize.create_pr") as mock_create_pr,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        outcome = finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=MagicMock(),
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    assert outcome == "failure"
    mock_create_pr.assert_not_called()
    linear_mock.set_state.assert_called_with("fake-linear-id", "Blocked")
    comment_body: str = linear_mock.post_comment.call_args[0][1]
    assert "WOR-456" in comment_body
    assert "checks_passed contract violation" in comment_body
    assert "PR creation aborted" in comment_body


def test_finalize_accepts_exact_required_checks(tmp_path: Path) -> None:
    """result.json with the manifest's exact required_checks → gate passes."""
    worker = _make_worker(tmp_path)
    _write_result(tmp_path, {"status": "success", "checks_passed": _REQUIRED})
    linear_mock = MagicMock()
    metrics_mock = MagicMock()

    with (
        patch(
            "app.core.watcher.watcher_finalize._validate_allowed_paths",
            return_value=[],
        ),
        patch("app.core.watcher.watcher_finalize.compute_tags", return_value=[]),
        patch(
            "app.core.watcher.watcher_finalize_helpers.run_checks",
            return_value=(True, []),
        ),
        patch("app.core.watcher.watcher_finalize_helpers.preserve_worker_artifacts"),
        patch(
            "app.core.watcher.watcher_finalize_helpers.fetch_sonar_findings",
            return_value=None,
        ),
        patch(
            "app.core.watcher.watcher_finalize.create_pr",
            return_value="https://github.com/example/pr/1",
        ) as mock_create_pr,
        patch("app.core.watcher.watcher_finalize.cleanup_worktree"),
    ):
        outcome = finalize_worker(
            worker,
            returncode=0,
            wall_time=1.0,
            linear=linear_mock,
            metrics=metrics_mock,
            escalation_policy=EscalationPolicy.from_toml(),
            repo_root=tmp_path,
            mode="default",
            project_id="proj",
        )

    # Gate did not fire: PR attempted, ticket never set to Blocked.
    assert outcome == "success"
    mock_create_pr.assert_called_once()
    blocked_calls = [
        c for c in linear_mock.set_state.call_args_list if c.args[1] == "Blocked"
    ]
    assert blocked_calls == []
