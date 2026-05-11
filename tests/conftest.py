"""Shared test fixtures for watcher sub-module tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher_types import ActiveWorker

# WOR-426: Block any test from spawning a real `claude` subprocess.
#
# WOR-312's retry-loop tests in tests/test_watcher_finalize.py mock run_checks
# but several forgot to mock launch_worker too — the retry path fires for real,
# spawning a real claude binary against vLLM and writing to production
# .claude/artifacts/wor_*/ paths. CI passes because the claude binary doesn't
# exist in CI runners; local devs paid the cost silently.
#
# Patch Popen.__init__ rather than the class itself so MagicMock(spec=...) still
# resolves the real attribute list.
_REAL_POPEN_INIT = subprocess.Popen.__init__


def _extract_first_arg(args: Any) -> str:
    if isinstance(args, (list, tuple)) and args:
        return str(args[0])
    if isinstance(args, str) and args:
        return args.split()[0]
    return ""


@pytest.fixture(autouse=True)
def _block_real_claude_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to spawn a real `claude` binary from any test."""

    def _guarded_init(
        self: subprocess.Popen[Any],
        args: Any,
        *rest: Any,
        **kwargs: Any,
    ) -> None:
        first = _extract_first_arg(args)
        binary = Path(first).name.lower() if first else ""
        if binary in ("claude", "claude.exe"):
            raise AssertionError(
                "Test attempted to spawn real `claude` binary "
                f"(argv[0]={first!r}). Mock launch_worker via "
                "patch('app.core.watcher.watcher_finalize.launch_worker', "
                "return_value=MagicMock()) inside the test's with-block."
            )
        _REAL_POPEN_INIT(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_init)


# Session-scoped QApplication fixture (pytest-qt)
@pytest.fixture(scope="session")
def qapp():
    """Create a single QApplication instance for the test session.

    Guards against missing Qt so the fixture skips gracefully when
    pytest-qt is not installed in the dev environment.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 not installed — Qt fixture unavailable")

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    app.quit()


def make_manifest(**overrides: object) -> ExecutionManifest:
    defaults: dict[str, object] = {
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
        # WOR-378: dispatch refuses manifests with empty required_checks, so
        # the conftest fixture defaults a non-empty list. Tests that exercise
        # the empty-required_checks path can override explicitly.
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)  # type: ignore[arg-type]


_SENTINEL: list[str] = ["app/core/bar.py"]


def make_active_worker(
    ticket_id: str = "WOR-11", allowed_paths: list[str] | None = None
) -> ActiveWorker:
    paths = _SENTINEL if allowed_paths is None else allowed_paths
    manifest = make_manifest(
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
