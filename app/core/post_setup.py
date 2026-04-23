import json
import re
import subprocess  # nosec B404 — controlled calls with hardcoded command lists, no shell=True
import urllib.error
import urllib.request
from pathlib import Path

from jinja2 import Environment, Undefined

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
