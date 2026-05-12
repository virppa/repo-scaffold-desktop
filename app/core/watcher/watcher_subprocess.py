"""Worker subprocess management for the watcher sub-system.

Stateless functions that launch and query worker processes. No persistent
state — each function takes all inputs as parameters.
This module may import from watcher_helpers and watcher_types (no other siblings).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import ssl
import subprocess  # nosec B404
import sys
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from app.core.manifest import ExecutionManifest
from app.core.metrics import CheckRunEntry, MetricsStore

from .watcher_helpers import (
    _tee_worker_output,
    build_worker_cmd,
    build_worker_env,
)
from .watcher_types import _CLAUDE_DIR

logger = logging.getLogger(__name__)

# Regex for git diff --shortstat output like:
#   "3 files changed, 45 insertions(+), 12 deletions(-)"
_SHORTSTAT_RE = re.compile(
    r"(\d+) files? changed"
    r"(?:, (\d+) insertions?\(\+\))?"
    r"(?:, (\d+) deletions?\(-\))?"
)

_SONAR_MAX_PAGES = 10
_LINEAR_MCP = '{"mcpServers":{"linear-server":{"type":"http","url":"https://mcp.linear.app/mcp"}}}'


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
    worker_verbose: bool = False,
    extra_constraint: str | None = None,
) -> subprocess.Popen[bytes]:
    """Launch a worker subprocess and return the Popen handle."""
    prompt = expand_skill(repo_root, manifest.ticket_id)

    if extra_constraint:
        prompt = f"RETRY: {extra_constraint}\n\n{prompt}"

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
        manifest.ticket_id,
        effective_mode,
        worktree_path,
        prompt,
        disallowed_tools,
        mcp_config_json=_LINEAR_MCP,
        effort=manifest.effort,
    )
    env = build_worker_env(
        effective_mode,
        dict(os.environ),
        quality_check_budget=len(manifest.required_checks),
    )

    log_path = worktree_path / f".claude/worker_{manifest.ticket_id.lower()}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "wb")

    if worker_verbose:
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


def _run_single_check(
    check_cmd: str,
    worktree_path: Path,
    check_env: dict[str, str],
) -> tuple[str, subprocess.CompletedProcess[str], float]:
    """Run one check command in the worktree. Returns (cmd, result, duration).

    Helper extracted so run_checks can submit multiple checks to a thread
    pool concurrently (WOR-467). subprocess.run is fine to call from
    multiple threads — each spawns a separate OS process.
    """
    logger.info("Running check: %s", check_cmd)
    start = datetime.now(timezone.utc).timestamp()
    result = subprocess.run(  # nosec B603
        shlex.split(check_cmd),
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        env=check_env,
    )
    duration = datetime.now(timezone.utc).timestamp() - start
    return check_cmd, result, duration


def run_checks(
    manifest: ExecutionManifest,
    worktree_path: Path,
    *,
    metrics: MetricsStore | None = None,
    ticket_id: str = "",
    project_id: str = "",
) -> tuple[bool, list[dict[str, int | str]]]:
    """Run manifest.required_checks in the worktree concurrently (WOR-467).

    Returns (all_passed, failed_checks) where *failed_checks* is a list of
    ``{check: str, exit_code: int}`` for each check that returned non-zero,
    in deterministic manifest order.

    WOR-467: the standard 4 checks (ruff/mypy/pytest/lint-imports) are
    independent — none reads another's output. They now run in a
    ThreadPoolExecutor with one slot per check. Total wall ≈ max(check
    walls) + small overhead, not the sum. On the post-WOR-464 baseline
    that's ~34s (pytest-bound) instead of ~37s serial.

    Output ordering and accumulation remain deterministic — futures are
    drained in `manifest.required_checks` order, so logger output and
    `failed_checks` list both match the serial behavior. `last_failure.json`
    is written for the FIRST manifest-order failure (matches old "first
    failure wins" semantics even though concurrent runs may finish in
    any order).

    When *metrics* is provided, each check execution is recorded to the
    check_run_log table via ``MetricsStore.record_check_run``. Concurrent
    recording is safe — MetricsStore opens a fresh SQLite connection per
    `_connect` context and SQLite handles concurrent writes via file lock.
    """
    artifact_dir = worktree_path / Path(manifest.artifact_paths.result_json).parent
    failure_artifact = artifact_dir / _LAST_FAILURE_FILENAME

    check_env = os.environ.copy()
    # WOR-398: WOR-391's skipif on tests/test_contribute_skills_workflow.py only
    # fires when WATCHER_WORKER=1. The worker subprocess gets that flag from
    # build_worker_env, but the watcher's post-worker required_checks step runs
    # in a fresh subprocess that inherits the daemon's env, where the flag is
    # absent — so the skip misses and pytest fails on contribute-skills tests.
    check_env["WATCHER_WORKER"] = "1"

    # Submit all checks concurrently. Empty required_checks → no executor.
    checks = list(manifest.required_checks)
    if not checks:
        if failure_artifact.exists():
            failure_artifact.unlink()
        return True, []

    results: dict[str, tuple[subprocess.CompletedProcess[str], float]] = {}
    with ThreadPoolExecutor(
        max_workers=len(checks), thread_name_prefix="run-checks"
    ) as ex:
        futures = {
            ex.submit(_run_single_check, cmd, worktree_path, check_env): cmd
            for cmd in checks
        }
        for future in futures:
            cmd, result, duration = future.result()
            results[cmd] = (result, duration)

    # Process in manifest order so output / metrics / last_failure.json
    # write order is deterministic regardless of thread completion order.
    failed_checks: list[dict[str, int | str]] = []
    first_failure_written = False
    for check_cmd in checks:
        result, duration = results[check_cmd]
        if metrics is not None and ticket_id:
            metrics.record_check_run(
                CheckRunEntry(
                    ticket_id=ticket_id,
                    project_id=project_id,
                    check_cmd=check_cmd,
                    outcome="passed" if result.returncode == 0 else "failed",
                    duration_s=round(duration, 3),
                )
            )
        if result.returncode != 0:
            logger.error(
                "Check failed: %s\n%s", check_cmd, result.stdout + result.stderr
            )
            failed_checks.append({"check": check_cmd, "exit_code": result.returncode})
            if not first_failure_written:
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
                first_failure_written = True

    if not failed_checks and failure_artifact.exists():
        failure_artifact.unlink()

    return (len(failed_checks) == 0, failed_checks)


def _rebase_onto_base(manifest: ExecutionManifest, worktree_path: Path) -> None:
    """Fetch origin/<base_branch> and rebase the worker branch onto it (WOR-445).

    On clean rebase: returns silently.
    On conflict or fetch failure: aborts any in-progress rebase, then raises
    CalledProcessError with a descriptive stderr — finalize_worker catches
    it and marks the ticket Blocked with the reason.
    """
    base = manifest.base_branch
    try:
        subprocess.run(  # nosec B603 B607
            ["git", "fetch", "origin", base],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(  # nosec B603 B607
            ["git", "rebase", f"origin/{base}"],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        # Best-effort cleanup so the worktree isn't left mid-rebase.
        subprocess.run(  # nosec B603 B607
            ["git", "rebase", "--abort"],
            cwd=str(worktree_path),
            check=False,
            capture_output=True,
            text=True,
        )
        stderr = (exc.stderr or exc.stdout or "").strip()
        raise subprocess.CalledProcessError(
            exc.returncode,
            exc.cmd,
            output=exc.output,
            stderr=(
                f"Rebase of {manifest.worker_branch} onto origin/{base} "
                f"failed (WOR-445): {stderr}. Resolve manually: "
                f"`cd <worktree>; git rebase origin/{base}` and push."
            ),
        ) from exc


def _find_existing_pr_url(
    exc: subprocess.CalledProcessError,
    manifest: ExecutionManifest,
    worktree_path: Path,
) -> str | None:
    """Recover from 'PR already exists' (WOR-444).

    Returns the existing PR's URL if the failure was a duplicate-PR error
    and we successfully looked up the existing PR. Returns None otherwise
    (caller should re-raise).
    """
    stderr = (exc.stderr or "").lower()
    if "already exists" not in stderr:
        return None
    try:
        listing = subprocess.run(  # nosec B603 B607
            [
                "gh",
                "pr",
                "list",
                "--head",
                manifest.worker_branch,
                "--state",
                "open",
                "--json",
                "url",
                "--jq",
                ".[0].url",
            ],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    url = listing.stdout.strip()
    if not url:
        return None
    logger.info(
        "PR already exists for %s — using existing %s (WOR-444 recovery)",
        manifest.worker_branch,
        url,
    )
    return url


def create_pr(manifest: ExecutionManifest, worktree_path: Path) -> str:
    """Push the worker branch and open a GitHub PR.

    Two safety steps run before `gh pr create`:

    - **WOR-445 — rebase off origin/<base_branch>.** Catches main-side changes
      that landed during the worker session so the PR opens against an
      up-to-date base. Avoids merge conflicts at PR-merge time (which
      otherwise force manual resolution per the WOR-132 incident).
      Rebase conflicts raise CalledProcessError → finalize_worker marks
      the ticket Blocked with a descriptive reason.

    - **WOR-444 — PR-already-exists recovery.** If `gh pr create` fails with
      "a pull request for branch ... already exists", re-fetch the
      existing PR's URL via `gh pr list --head` and treat it as success.
      The duplicate-PR error is downgraded from finalize failure (Blocked
      in Linear) to an INFO log. Defense in depth alongside the worker
      hook that blocks worker-side `gh pr create` in the first place.

    Auto-merge is enabled only when targeting an epic branch. PRs targeting
    main are left open for human review — auto-merging to main is forbidden.
    """
    _rebase_onto_base(manifest, worktree_path)
    # Force-with-lease handles the post-rebase case where the worker may
    # have already pushed an earlier head (pre-WOR-444 behaviour). Safe on
    # first push too: missing remote ref is a no-op for the lease check.
    subprocess.run(  # nosec B603 B607
        [
            "git",
            "push",
            "-u",
            "--force-with-lease",
            "origin",
            manifest.worker_branch,
        ],
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
    try:
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
    except subprocess.CalledProcessError as exc:
        # WOR-444 recovery: worker (or earlier finalize attempt) already
        # opened a PR for this branch — re-fetch and proceed as if we
        # created it ourselves.
        existing = _find_existing_pr_url(exc, manifest, worktree_path)
        if existing is None:
            raise
        pr_url = existing

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


def _build_sonar_url(
    token: str, project_key: str, branch: str, page: int
) -> tuple[urllib.request.Request, str]:
    """Build the SonarCloud API request URL and request object."""
    creds = base64.b64encode(f"{token}:".encode()).decode()
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
    return req, url


def _parse_sonar_response(
    raw_response: bytes, all_severities: list[str], page: int
) -> tuple[list[str], int, bool]:
    """Parse a single page of SonarCloud JSON response.

    Returns ``(all_severities, total, should_break)`` where
    *all_severities* is extended with this page's severity values,
    *total* is the total issue count from the API, and
    *should_break* is True when pagination is complete.
    """
    data: dict[str, object] = json.loads(raw_response)
    issues = data.get("issues") or []
    all_severities.extend(
        str(issue["severity"])
        for issue in (issues if isinstance(issues, list) else [])
        if isinstance(issue, dict) and issue.get("severity")
    )
    raw_total = data.get("total")
    total = int(raw_total) if isinstance(raw_total, int) else 0
    return all_severities, total, page * 500 >= total


def fetch_sonar_findings(branch: str) -> list[str] | None:
    """Return per-severity finding list from SonarCloud for *branch*, or None.

    Returns a list of severity strings (e.g. ['BLOCKER', 'CRITICAL']) or None
    when SONAR_TOKEN / SONAR_PROJECT_KEY are absent or the API call fails. An
    empty list means the branch was scanned and has no open issues.
    """
    token = os.environ.get("SONAR_TOKEN")
    project_key = os.environ.get("SONAR_PROJECT_KEY")
    if not token or not project_key:
        return None

    ctx = ssl.create_default_context()
    all_severities: list[str] = []

    for page in range(1, _SONAR_MAX_PAGES + 1):
        try:
            req, _ = _build_sonar_url(token, project_key, branch, page)
            with urllib.request.urlopen(  # nosec B310  # nosemgrep
                req, timeout=10, context=ctx
            ) as resp:
                all_severities, _total, should_break = _parse_sonar_response(
                    resp.read(), all_severities, page
                )
            if should_break:
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


def parse_git_shortstat(diff_output: str) -> tuple[int, int]:
    """Parse ``git diff --shortstat`` output into (lines_changed, files_changed).

    Returns ``(0, 0)`` when the diff output is empty or does not match the
    expected pattern (e.g. no commits since base).
    """
    if not diff_output.strip():
        return 0, 0
    m = _SHORTSTAT_RE.search(diff_output)
    if m is None:
        return 0, 0
    files = int(m.group(1))
    ins = int(m.group(2)) if m.group(2) is not None else 0
    dels = int(m.group(3)) if m.group(3) is not None else 0
    return ins + dels, files
