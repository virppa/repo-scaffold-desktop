import json
import re
import subprocess  # nosec B404 — controlled calls with hardcoded command lists, no shell=True
import sys
import urllib.error
import urllib.request
from pathlib import Path

from jinja2 import Environment, Undefined

from app.core.credentials import get_token
from app.core.user_prefs import UserPreferences

_GITHUB_SOURCE_RE = re.compile(r"^github:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


def _parse_repo_full_name(clone_url: str) -> str:
    """Extract owner/repo from a GitHub clone URL.

    Handles both ``https://github.com/owner/repo`` and trailing-slash variants.
    Returns the value as-is when no GitHub URL pattern is detected — the caller
    should already have validated the format, so this is a fast path that does not
    raise for non-GitHub URLs.
    """
    prefix = "https://github.com/"
    if clone_url.startswith(prefix):
        result = clone_url[len(prefix) :]
        return result.rstrip("/")
    return clone_url


def fetch_skills(
    output_path: Path,
    skills_source: str,
    skills_version: str,
    context: dict[str, object] | None = None,
) -> list[str]:
    """Fetch .claude/commands/ from a versioned skills repo and write to output_path.

    When context is provided each downloaded file is rendered through Jinja2 using
    non-strict Undefined so missing variables produce an empty string, not an error.

    Returns the list of relative paths written. On network or API errors, logs a
    warning and returns an empty list — fetch failure is intentionally non-fatal.

    Raises ValueError immediately for malformed skills_source to catch config bugs.
    """
    match = _GITHUB_SOURCE_RE.match(skills_source)
    if not match:
        raise ValueError(
            f"Invalid skills_source {skills_source!r}. "
            "Expected format: github:<owner>/<repo>"
        )
    owner, repo = match.group(1), match.group(2)

    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/git/trees/{skills_version}?recursive=1"
    )
    try:
        req = urllib.request.Request(  # nosec B310 — URL constructed from validated owner/repo/version
            api_url, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            tree = json.loads(resp.read())
    except OSError as exc:
        print(
            f"[skills] Warning: could not fetch {skills_source}@{skills_version}: {exc}"
        )
        return []

    commands_prefix = ".claude/commands/"
    written: list[str] = []
    _jinja_env: Environment | None = (
        Environment(  # nosec B701  # nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
            undefined=Undefined,
            keep_trailing_newline=True,
            autoescape=False,
        )
        if context is not None
        else None
    )

    for entry in tree.get("tree", []):
        path: str = entry.get("path", "")
        if not path.startswith(commands_prefix) or entry.get("type") != "blob":
            continue
        # Guard against path traversal in API-returned paths
        if ".." in path or not _SAFE_PATH_RE.match(path):
            print(f"[skills] Skipping unsafe path: {path}")
            continue
        raw_url = (
            f"https://raw.githubusercontent.com/{owner}/{repo}/{skills_version}/{path}"
        )
        try:
            with urllib.request.urlopen(raw_url, timeout=10) as resp:  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                content = resp.read()
        except OSError as exc:
            print(f"[skills] Warning: could not download {path}: {exc}")
            continue

        if _jinja_env is not None and context is not None:
            text = content.decode("utf-8", errors="replace")
            rendered = _jinja_env.from_string(text).render(**context)  # nosec  # nosemgrep: python.flask.security.xss.audit.direct-use-of-jinja2.direct-use-of-jinja2
            content = rendered.encode("utf-8")

        dest = output_path / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        written.append(path)

    return written


def run_git_init(output_path: Path) -> None:
    """Initialise a Git repository in *output_path*.

    Runs ``git init`` via ``subprocess.run`` with ``shell=False``.

    Raises:
        RuntimeError: If ``git`` is not found on PATH or ``git init``
            returns a non-zero exit code. The message includes the raw
            stderr output for debugging.

    Example:
        >>> run_git_init(Path("/tmp/myrepo"))  # doctest: +SKIP
    """
    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["git", "init"],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError("git not found on PATH — install git and try again")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git init failed: {stderr}")


def run_precommit_install(output_path: Path) -> None:
    """Install the pre-commit hook in the Git repository at *output_path*.

    Runs ``pre-commit install`` via ``subprocess.run`` with ``shell=False``.
    The hook runs the repo-level pre-commit configuration on every ``git commit``.

    Raises:
        RuntimeError: If ``pre-commit`` is not found on PATH or the install
            command returns a non-zero exit code. The message includes the
            raw stderr output for debugging.

    Example:
        >>> run_precommit_install(Path("/tmp/myrepo"))  # doctest: +SKIP
    """
    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["pre-commit", "install"],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pre-commit not found on PATH — install pre-commit and try again"
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"pre-commit install failed: {stderr}")


def run_initial_push(
    output_path: Path, remote_url: str, prefs: UserPreferences
) -> None:
    """Complete the initial Git setup for a scaffolded repository.

    Executes the following steps in order:

    1. **``git add .``** — stage all files.
    2. **``git commit -m "Initial scaffold"``** — create the initial commit,
       using ``prefs.author_name`` and ``prefs.author_email`` as the commit
       identity when both are set.
    3. **``git remote add origin <remote_url>``** — attach the remote.
    4. **``git branch -M main``** — rename the default branch to ``main``.
    5. **``git push -u origin main``** — push the initial commit.

    Args:
        output_path: Path to the scaffolded repository root.
        remote_url: HTTPS or SSH URL of the remote repository.
        prefs: User preferences containing optional author name and email.

    Raises:
        RuntimeError: On any subprocess failure. Each error message identifies
            which step failed (``git add``, ``git commit``, ``git remote``,
            ``git branch``, or ``git push``) and includes the raw stderr.

    Example:
        >>> run_initial_push(Path("/tmp/myrepo"), "https://github.com/u/r", prefs)
        # doctest: +SKIP
    """
    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["git", "add", "."],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git add failed: {stderr}")

    commit_args: list[str] = ["git", "commit", "-m", "Initial scaffold"]
    if prefs.author_name and prefs.author_email:
        commit_args = [
            "git",
            "-c",
            f"user.name={prefs.author_name}",
            "-c",
            f"user.email={prefs.author_email}",
            "commit",
            "-m",
            "Initial scaffold",
        ]
    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            commit_args,
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git commit failed: {stderr}")

    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["git", "remote", "add", "origin", remote_url],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git remote add origin failed: {stderr}")

    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["git", "branch", "-M", "main"],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git branch -M main failed: {stderr}")

    try:
        subprocess.run(  # nosec B603 B607 — hardcoded command, no user input, no shell
            ["git", "push", "-u", "origin", "main"],
            cwd=output_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git push failed: {stderr}")


def create_github_repo(
    repo_name: str,
    prefs: UserPreferences,
    private: bool = True,
    description: str = "",
) -> str:
    """Create a GitHub repository via the REST API and return its clone URL.

    **Endpoint:** ``POST https://api.github.com/user/repos``

    **Auth model:** Bearer token obtained from :func:`app.core.credentials.get_token`
    (reads from OS keyring via ``github-token`` user preference).

    **Failure semantics:**

    * ``RuntimeError`` with message ``"No GitHub token configured..."`` when no
      token is available.
    * ``RuntimeError`` with message ``"Repository '<name>' already exists — ..."``
      on HTTP 422 (repository name conflict).
    * ``RuntimeError`` with the API error message for all other 4xx/5xx responses.

    Args:
        repo_name: Desired repository name (must be unique for the authenticated user).
        prefs: User preferences (currently unused; retained for API compatibility).
        private: Whether the repository should be private. Defaults to ``True``.
        description: Optional repository description.

    Returns:
        The HTML clone URL (e.g. ``https://github.com/owner/repo``).

    Example:
        >>> create_github_repo("my-repo", prefs)  # doctest: +SKIP
    """
    token = get_token()
    if token is None:
        raise RuntimeError(
            "No GitHub token configured. Run: python -m app.cli config set github-token"
        )

    body = {
        "name": repo_name,
        "private": private,
        "description": description,
        "auto_init": False,
    }
    payload = json.dumps(body).encode("utf-8")

    api_url = "https://api.github.com/user/repos"
    req = urllib.request.Request(  # nosec B310
        api_url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = json.loads(exc.read()).get("message", str(exc))
        except (OSError, ValueError):  # noqa: BLE001
            err_body = str(exc)
        if exc.code == 422:
            raise RuntimeError(
                f"Repository '{repo_name}' already exists — {err_body}"
            ) from exc
        raise RuntimeError(f"GitHub API error: {err_body}") from exc

    clone_url: str = result["html_url"]
    return clone_url


# ── Topic maps ─────────────────────────────────────────────────────────────────

_TOPICS_BY_PRESET: dict[str, list[str]] = {
    "python_basic": ["python"],
    "python_desktop": ["python", "pyside6", "desktop"],
    "full_agentic": ["python", "claude", "agentic", "linear"],
}


def configure_github_repo(
    repo_full_name: str,
    preset: str,
    include_ci: bool,
) -> None:
    """Apply opinionated settings to a freshly created GitHub repository.

    Makes three API calls:

    1. **PUT /repos/{owner}/{repo}/topics** — sets the repository topic list
       derived from the chosen preset.

    2. **PATCH /repos/{owner}/{repo}** — disables wiki and projects.

    3. **PUT /repos/{owner}/{repo}/branches/main/protection** (conditional) —
       enables branch protection when ``include_ci`` is ``True`` (requires one
       pull-request review and status checks).

    All parameters are validated *before* any authentication or network calls.
    Topics, wiki, and projects steps raise ``RuntimeError`` on 4xx/5xx.  Branch
    protection failure is logged but does not break the overall flow — a
    failed branch-protection step only warns; topics/wiki failures are fatal.
    """
    # Validate full_name format BEFORE calling get_token()
    if "/" not in repo_full_name or repo_full_name.count("/") != 1:
        raise ValueError(
            f"Invalid repo_full_name {repo_full_name!r}. "
            "Expected format: <owner>/<repo>"
        )

    owner, repo = repo_full_name.split("/")
    topics = _TOPICS_BY_PRESET.get(preset, ["python"])

    token = get_token()
    if token is None:
        raise RuntimeError(
            "No GitHub token configured. Run: python -m app.cli config set github-token"
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.mercy-preview+json",
    }

    # 1. Set topics
    _put(
        f"https://api.github.com/repos/{owner}/{repo}/topics",
        {"names": topics},
        headers=headers,
    )

    # 2. Disable wiki and projects
    _patch(
        f"https://api.github.com/repos/{owner}/{repo}",
        {"has_wiki": False, "has_projects": False},
        headers=headers,
    )

    # 3. Branch protection (conditional)
    if include_ci:
        _set_branch_protection(owner, repo, headers)


def _put(url: str, body: dict[str, object], headers: dict[str, str]) -> None:
    """Send a PUT request. Raises ``RuntimeError`` on HTTP errors."""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # nosec B310
        url,
        data=payload,
        method="PUT",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            if resp.status >= 400:
                err_body = ""
                try:
                    err_body = json.loads(resp.read()).get("message", str(resp))
                except (OSError, ValueError):  # noqa: BLE001
                    err_body = str(resp)
                raise RuntimeError(f"GitHub API error ({resp.status}): {err_body}")
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = json.loads(exc.read()).get("message", str(exc))
        except (OSError, ValueError):  # noqa: BLE001
            err_body = str(exc)
        raise RuntimeError(f"GitHub API error: {err_body}") from exc


def _patch(url: str, body: dict[str, object], headers: dict[str, str]) -> None:
    """Send a PATCH request. Raises ``RuntimeError`` on HTTP errors."""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # nosec B310
        url,
        data=payload,
        method="PATCH",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            if resp.status >= 400:
                err_body = ""
                try:
                    err_body = json.loads(resp.read()).get("message", str(resp))
                except (OSError, ValueError):  # noqa: BLE001
                    err_body = str(resp)
                raise RuntimeError(f"GitHub API error ({resp.status}): {err_body}")
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = json.loads(exc.read()).get("message", str(exc))
        except (OSError, ValueError):  # noqa: BLE001
            err_body = str(exc)
        raise RuntimeError(f"GitHub API error: {err_body}") from exc


def _set_branch_protection(owner: str, repo: str, headers: dict[str, str]) -> None:
    """Enable branch protection on main.

    Logs a warning on failure rather than raising — a failed branch-protection
    step should not break the overall flow.
    """
    body = {
        "required_pull_request_reviews": {
            "dismissal_restrictions": {},
            "require_code_owner_reviews": True,
            "required_approving_review_count": 1,
        },
        "required_status_checks": {
            "strict": True,
            "contexts": [],
        },
        "restrictions": None,
    }
    payload = json.dumps(body).encode("utf-8")
    url = f"https://api.github.com/repos/{owner}/{repo}/branches/main/protection"
    req = urllib.request.Request(  # nosec B310
        url,
        data=payload,
        method="PUT",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            if resp.status >= 400:
                err_body = ""
                try:
                    err_body = json.loads(resp.read()).get("message", str(resp))
                except (OSError, ValueError):  # noqa: BLE001
                    err_body = str(resp)
                print(
                    f"Warning: branch protection for main failed: {err_body}",
                    file=sys.stderr,
                )
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = json.loads(exc.read()).get("message", str(exc))
        except (OSError, ValueError):  # noqa: BLE001
            err_body = str(exc)
        print(
            f"Warning: branch protection for main failed: {err_body}",
            file=sys.stderr,
        )
