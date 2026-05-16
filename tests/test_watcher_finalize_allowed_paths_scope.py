"""WOR-521 regression: _validate_allowed_paths must scope to the worker's
OWN commits, immune to the base branch advancing after the worker branched
(sibling / earlier-merged ticket). Builds a real temp git repo with an
`origin` remote and a base that advances post-branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.core.watcher.watcher_finalize_helpers import _validate_allowed_paths
from tests.conftest import make_manifest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_commit(repo: Path, rel: str, content: str, msg: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg)


def test_validate_allowed_paths_ignores_base_advancement(tmp_path: Path) -> None:
    """Base advances with an UNRELATED file after the worker branched →
    that file must NOT be reported as this ticket's violation (WOR-521)."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "clone", str(origin), str(wt)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(wt, "symbolic-ref", "HEAD", "refs/heads/main")
    _write_commit(wt, "base_file.py", "x = 0\n", "C0 base")
    _git(wt, "branch", "-M", "main")
    _git(wt, "push", "origin", "main")

    # worker branches off C0
    _git(wt, "checkout", "-b", "wor-521-worker")
    _write_commit(wt, "app/core/mine.py", "y = 1\n", "worker change")

    # base advances on origin with an UNRELATED file (the sibling / 520)
    _git(wt, "checkout", "main")
    _write_commit(wt, "app/core/watcher/watcher_worktrees.py", "z=2\n", "sibling")
    _git(wt, "push", "origin", "main")

    # worker is NOT rebased; local `main` ref now also points past C0
    _git(wt, "checkout", "wor-521-worker")

    manifest = make_manifest(
        ticket_id="WOR-521",
        base_branch="main",
        worker_branch="wor-521-worker",
        allowed_paths=["app/core/mine.py"],
        forbidden_paths=[],
    )

    violations = _validate_allowed_paths(manifest, wt)

    # The sibling/base-advanced file must NOT be flagged; the worker's own
    # in-scope file is allowed → zero violations.
    assert violations == [], f"base-advancement contaminated scope: {violations}"


def test_validate_allowed_paths_still_flags_worker_out_of_scope(
    tmp_path: Path,
) -> None:
    """The fix must NOT weaken real enforcement: a file the WORKER itself
    touched outside allowed_paths is still flagged."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "clone", str(origin), str(wt)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(wt, "symbolic-ref", "HEAD", "refs/heads/main")
    _write_commit(wt, "base_file.py", "x = 0\n", "C0 base")
    _git(wt, "branch", "-M", "main")
    _git(wt, "push", "origin", "main")

    _git(wt, "checkout", "-b", "wor-521-worker")
    _write_commit(wt, "app/core/mine.py", "y = 1\n", "in scope")
    _write_commit(wt, "app/ui/forbidden.py", "u = 9\n", "OUT of scope")

    manifest = make_manifest(
        ticket_id="WOR-521",
        base_branch="main",
        worker_branch="wor-521-worker",
        allowed_paths=["app/core/mine.py"],
        forbidden_paths=[],
    )

    violations = _validate_allowed_paths(manifest, wt)

    assert any("app/ui/forbidden.py" in v for v in violations), violations
    assert not any("app/core/mine.py" in v for v in violations), violations
