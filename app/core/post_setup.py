import json
import re
import subprocess  # nosec B404 — controlled calls with hardcoded command lists, no shell=True
import urllib.error
import urllib.request
from pathlib import Path

from jinja2 import Environment, Undefined

from app.core.credentials import get_token
from app.core.user_prefs import UserPreferences

_GITHUB_SOURCE_RE = re.compile(r"^github:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


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
    """Run `git init` in output_path. Raises RuntimeError on failure."""
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
    """Run `pre-commit install` in output_path. Raises RuntimeError on failure."""
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
    """Stage all files, create initial commit, set remote origin, and push to main.

    Raises ``RuntimeError`` on any subprocess failure with a clear message
    including stderr.
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

    Requires a GitHub token configured via :func:`app.core.credentials.get_token`.
    Raises ``RuntimeError`` if no token is configured or the API returns an error.
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
        except Exception:  # noqa: BLE001
            err_body = str(exc)
        if exc.code == 422:
            raise RuntimeError(
                f"Repository '{repo_name}' already exists — {err_body}"
            ) from exc
        raise RuntimeError(f"GitHub API error: {err_body}") from exc

    clone_url: str = result["html_url"]
    return clone_url
