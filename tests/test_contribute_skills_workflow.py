"""End-to-end tests for scripts/contribute_skills.sh.

The script is driven via subprocess with stubbed environment variables.
No SKILLS_REPO_PAT or network access is required — only a local git repo.
"""

import os
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "contribute_skills.sh"


def _git(repo: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in *repo* and return the result."""
    return subprocess.run(
        ["git"] + list(args),
        cwd=repo,
        check=True,
        capture_output=capture,
        text=True,
    )


def _setup_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a bare git repo at *tmp_path* / *repo* with *files* and return it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".claude" / "commands").mkdir(parents=True)
    for rel_path, content in files.items():
        full = repo / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@test")
    _git(repo, "config", "user.name", "CI")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _env(
    tmp_path: Path,
    repo: Path,
    skills_repo: Path,
    before: str,
    after: str,
) -> dict:
    """Build env dict with SKILLS_REPO_PATH pointing *away* from the source repo."""
    skills_repo.mkdir(exist_ok=True)
    return {
        **os.environ,
        "BEFORE": before,
        "AFTER": after,
        "GITHUB_WORKSPACE": str(repo),
        "SKILLS_REPO_PATH": str(skills_repo),
        "SOURCE_REPO": "owner/repo",
        "SOURCE_SHA": after,
        "GH_TOKEN": "dummy",
    }


# --- 1. Normal push — two files changed ------------------------------------


def test_normal_push_detects_changed_files():
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        repo = _setup_repo(
            tmp_path,
            {
                ".claude/commands/python_basic.py": "# python_basic\n",
                ".claude/commands/go_basic.py": "# go_basic\n",
            },
        )

        # Second commit with changes to both files
        (repo / ".claude/commands/python_basic.py").write_text(
            "# python_basic updated\n"
        )
        (repo / ".claude/commands/go_basic.py").write_text("# go_basic new content\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "update")
        after_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        _git(repo, "reset", "--hard", "HEAD~1")
        before_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        skills_repo = tmp_path / "skills-repo"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=_env(tmp_path, repo, skills_repo, before_sha, after_sha),
            cwd=repo,
        )

        assert proc.returncode == 0
        assert "python_basic.py" in proc.stdout
        assert "go_basic.py" in proc.stdout
        assert "Changed files to contribute:" in proc.stdout


# --- 2. Initial branch push — BEFORE is all zeros --------------------------


def test_initial_branch_push():
    """BEFORE=0000… lists all tracked files (first push to a new branch)."""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        repo = _setup_repo(
            tmp_path,
            {
                ".claude/commands/python_basic.py": "# python_basic\n",
                ".claude/commands/go_basic.py": "# go_basic\n",
            },
        )

        skills_repo = tmp_path / "skills-repo"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=_env(tmp_path, repo, skills_repo, "0" * 40, "a" * 40),
            cwd=repo,
        )

        assert proc.returncode == 0
        assert "python_basic.py" in proc.stdout
        assert "go_basic.py" in proc.stdout


# --- 3. No-op push — no .claude/commands/ files match ----------------------


def test_noop_push_no_changes():
    """git diff returns nothing → script exits 0 with no-op message."""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        repo = _setup_repo(
            tmp_path,
            {
                ".claude/commands/python_basic.py": "# python_basic\n",
                "other.txt": "not a skill\n",
            },
        )

        # Change a file OUTSIDE .claude/commands/
        (repo / "other.txt").write_text("modified\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "unrelated")
        after_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        before_sha = _git(repo, "rev-parse", "HEAD~1").stdout.strip()

        skills_repo = tmp_path / "skills-repo"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=_env(tmp_path, repo, skills_repo, before_sha, after_sha),
            cwd=repo,
        )

        assert proc.returncode == 0
        assert "No .claude/commands/ files changed" in proc.stdout


# --- 4. Deleted source file — skips missing files ---------------------------


def test_deleted_source_file_skipped():
    """File in CHANGED_FILES but missing on disk → printed as skipped, no crash."""
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        repo = _setup_repo(
            tmp_path,
            {
                ".claude/commands/python_basic.py": "# python_basic\n",
                ".claude/commands/go_basic.py": "# go_basic\n",
            },
        )

        # Update one file and delete the other
        (repo / ".claude/commands/python_basic.py").write_text("# updated\n")
        (repo / ".claude/commands/go_basic.py").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "update+delete")
        after_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        before_sha = _git(repo, "rev-parse", "HEAD~1").stdout.strip()

        skills_repo = tmp_path / "skills-repo"
        proc = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=_env(tmp_path, repo, skills_repo, before_sha, after_sha),
            cwd=repo,
        )

        assert proc.returncode == 0
        assert "go_basic.py" in proc.stdout
