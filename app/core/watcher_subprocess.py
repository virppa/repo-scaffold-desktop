"""Worker subprocess management for the watcher sub-system.

Stateless functions that launch and query worker processes. No persistent
state — each function takes all inputs as parameters.
This module may import from watcher_helpers and watcher_types (no other siblings).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess  # nosec B404
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from app.core.manifest import ExecutionManifest
from app.core.watcher_helpers import (
    _tee_worker_output,
    build_worker_cmd,
    build_worker_env,
)
from app.core.watcher_types import _CLAUDE_DIR

logger = logging.getLogger(__name__)

_SONAR_MAX_PAGES = 10


def expand_skill(repo_root: Path, ticket_id: str) -> str | None:
    """Return the implement-ticket skill content with $ARGUMENTS substituted.

    Returns None if the skill file cannot be read (caller falls back to
    the /implement-ticket shortcut).
    """
    skill_path = repo_root / _CLAUDE_DIR / "commands" / "implement-ticket.md"
    try:
        return skill_path.read_text(encoding="utf-8").replace("$ARGUMENTS", ticket_id)
    except OSError:
        logger.warning("Could not read skill file %s; using shortcut", skill_path)
        return None


def build_snippet_tool_restrictions(snippets: list[str]) -> list[str]:
    """Return --disallowed-tools patterns derived from context_snippets headers.

    Each snippet starts with a comment line like:
        # app/core/watcher.py lines 574-589
    We extract the basename and return glob patterns that block Read on those
    files regardless of the absolute path the worker uses.
    """
    seen: set[str] = set()
    patterns: list[str] = []
    header_re = re.compile(r"^#\s+(\S+)\s+lines?\s+\d")
    for snippet in snippets:
        first_line = snippet.splitlines()[0] if snippet else ""
        m = header_re.match(first_line)
        if m:
            basename = Path(m.group(1)).name
            if basename not in seen:
                seen.add(basename)
                patterns.append(f"Read(*{basename})")
    return patterns


def launch_worker(
    repo_root: Path,
    manifest: ExecutionManifest,
    worktree_path: Path,
    effective_mode: str,
    verbose: bool = False,
) -> subprocess.Popen[bytes]:
    """Launch a worker subprocess and return the Popen handle."""
    prompt = expand_skill(repo_root, manifest.ticket_id)

    disallowed_tools: list[str] | None = None
    if manifest.context_snippets and effective_mode == "cloud":
        disallowed_tools = build_snippet_tool_restrictions(manifest.context_snippets)
        if disallowed_tools and prompt:
            file_list = ", ".join(
                p.removeprefix("Read(*").removesuffix(")") for p in disallowed_tools
            )
            warning = (
                f"CRITICAL: The following files are pre-loaded as context_snippets "
                f"in the manifest: {file_list}. "
                f"DO NOT use the Read tool on these files — "
                f"the tool is blocked and attempting to read them will "
                f"abort the task. "
                f"Use only the snippets already provided.\n\n"
            )
            prompt = warning + (prompt or "")

    cmd = build_worker_cmd(
        manifest.ticket_id, effective_mode, worktree_path, prompt, disallowed_tools
    )
    env = build_worker_env(effective_mode, dict(os.environ))

    log_path = worktree_path / f".claude/worker_{manifest.ticket_id.lower()}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "wb")  # noqa: SIM115

    if verbose:
        prefix = f"[{manifest.ticket_id}] ".encode()
        process = subprocess.Popen(  # nosec B603 B607
            cmd,
            cwd=str(worktree_path),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if process.stdout is None:
            raise RuntimeError("process.stdout is None despite stdout=PIPE")
        stderr_buf: IO[bytes] = getattr(sys.stderr, "buffer", None) or sys.stderr.buffer
        threading.Thread(
            target=_tee_worker_output,
            args=(process.stdout, log_file, prefix, stderr_buf),
            daemon=True,
            name=f"tee-{manifest.ticket_id}",
        ).start()
        return process

    return subprocess.Popen(  # nosec B603 B607
        cmd,
        cwd=str(worktree_path),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


_LAST_FAILURE_FILENAME = "last_failure.json"


def run_checks(manifest: ExecutionManifest, worktree_path: Path) -> bool:
    """Run manifest.required_checks in the worktree. Returns True if all pass."""
    artifact_dir = worktree_path / Path(manifest.artifact_paths.result_json).parent
    failure_artifact = artifact_dir / _LAST_FAILURE_FILENAME

    all_passed = True
    for check_cmd in manifest.required_checks:
        logger.info("Running check: %s", check_cmd)
        result = subprocess.run(  # nosec B603
            shlex.split(check_cmd),
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                "Check failed: %s\n%s", check_cmd, result.stdout + result.stderr
            )
            all_passed = False
            artifact_dir.mkdir(parents=True, exist_ok=True)
            failure_artifact.write_text(
                json.dumps(
                    {
                        "failed_at": datetime.now(timezone.utc).isoformat(),
                        "check": check_cmd,
                        "stdout": result.stdout[:4000],
                        "stderr": result.stderr,
                    }
                ),
                encoding="utf-8",
            )

    if all_passed and failure_artifact.exists():
        failure_artifact.unlink()

    return all_passed


def create_pr(manifest: ExecutionManifest, worktree_path: Path) -> str:
    """Push the worker branch and open a GitHub PR.

    Auto-merge is enabled only when targeting an epic branch. PRs targeting
    main are left open for human review — auto-merging to main is forbidden.
    """
    subprocess.run(  # nosec B603 B607
        ["git", "push", "-u", "origin", manifest.worker_branch],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
        text=True,
    )
    ahead = subprocess.run(  # nosec B603 B607
        [
            "git",
            "log",
            f"origin/{manifest.base_branch}..{manifest.worker_branch}",
            "--oneline",
        ],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if not ahead.stdout.strip():
        raise subprocess.CalledProcessError(
            1,
            "git log",
            stderr=(
                f"No commits on {manifest.worker_branch} ahead of "
                f"{manifest.base_branch} — worker did not commit any changes"
            ),
        )
    result = subprocess.run(  # nosec B603 B607
        [
            "gh",
            "pr",
            "create",
            "--base",
            manifest.base_branch,
            "--head",
            manifest.worker_branch,
            "--title",
            f"{manifest.ticket_id} {manifest.title}",
            "--body",
            f"Closes {manifest.ticket_id}\n\n{manifest.done_definition}",
        ],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=True,
    )
    pr_url = result.stdout.strip()

    if manifest.base_branch == "main":
        logger.info(
            "PR %s targets main — leaving open for human review (no auto-merge)",
            pr_url,
        )
        return pr_url

    merge_result = subprocess.run(  # nosec B603 B607
        ["gh", "pr", "merge", "--auto", "--squash", pr_url],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if merge_result.returncode != 0:
        output = (merge_result.stderr or merge_result.stdout).strip()
        # "clean status" means no required checks on the target branch (epic
        # branches) — PR is already mergeable, so fall back to immediate merge.
        if "enablePullRequestAutoMerge" in output or "clean status" in output:
            logger.info(
                "No required checks on target branch — merging %s immediately",
                pr_url,
            )
            immediate = subprocess.run(  # nosec B603 B607
                ["gh", "pr", "merge", "--squash", pr_url],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if immediate.returncode != 0:
                imm_output = (immediate.stderr or immediate.stdout).strip()
                logger.warning(
                    "gh pr merge --squash also failed for %s (rc=%d): %s",
                    pr_url,
                    immediate.returncode,
                    imm_output,
                )
        else:
            logger.warning(
                "gh pr merge --auto failed for %s (rc=%d): %s",
                pr_url,
                merge_result.returncode,
                output,
            )
    return pr_url


def fetch_sonar_findings(branch: str) -> list[str] | None:
    """Return per-severity finding list from SonarCloud for *branch*, or None.

    Returns a list of severity strings (e.g. ['BLOCKER', 'CRITICAL']) or None
    when SONAR_TOKEN / SONAR_PROJECT_KEY are absent or the API call fails. An
    empty list means the branch was scanned and has no open issues.
    """
    import base64
    import ssl
    import urllib.parse
    import urllib.request

    token = os.environ.get("SONAR_TOKEN")
    project_key = os.environ.get("SONAR_PROJECT_KEY")
    if not token or not project_key:
        return None

    creds = base64.b64encode(f"{token}:".encode()).decode()
    ctx = ssl.create_default_context()
    all_severities: list[str] = []

    for page in range(1, _SONAR_MAX_PAGES + 1):
        params = urllib.parse.urlencode(
            {
                "componentKeys": project_key,
                "branch": branch,
                "resolved": "false",
                "ps": "500",
                "p": str(page),
            }
        )
        url = f"https://sonarcloud.io/api/issues/search?{params}"
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
        try:
            with urllib.request.urlopen(  # nosec B310  # nosemgrep
                req, timeout=10, context=ctx
            ) as resp:
                data: dict[str, object] = json.loads(resp.read())
            issues = data.get("issues") or []
            all_severities.extend(
                str(issue["severity"])
                for issue in (issues if isinstance(issues, list) else [])
                if isinstance(issue, dict) and issue.get("severity")
            )
            raw_total = data.get("total")
            total = int(raw_total) if isinstance(raw_total, int) else 0
            if page * 500 >= total:
                break
        except Exception:
            logger.debug(
                "Could not fetch Sonar findings for branch %s (page %d)",
                branch,
                page,
                exc_info=True,
            )
            if page == 1:
                return None
            break

    return all_severities
