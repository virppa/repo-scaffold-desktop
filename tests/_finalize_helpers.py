"""Shared test helpers for finalize_worker tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.core.escalation_policy import EscalationPolicy
from app.core.watcher.watcher_finalize import finalize_worker
from app.core.watcher.watcher_types import ActiveWorker

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
