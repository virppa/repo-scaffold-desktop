"""Cross-platform daemon launch + .env autoload helpers (WOR-435).

Three pure functions used by `app.cli.operator._run_watcher`:

- `load_env_file()` — read .env into os.environ before the watcher starts
- `launch_detached()` — spawn the watcher as a background subprocess and
  return its PID. Parent process exits; child continues independently.
- `launch_in_new_terminal()` — Windows-only convenience: open a visible
  cmd.exe window running the watcher attached, with env auto-loaded by
  virtue of the child invocation also calling `load_env_file()`.
"""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows CreateProcess flags — defined here so we don't need win32 deps.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def load_env_file(repo_root: Path | None = None) -> int:
    """Load .env from *repo_root* (default: cwd) into os.environ.

    Existing env vars take precedence — the .env file is a fallback. Returns
    the count of variables loaded (0 if no .env file exists, also 0 if all
    keys were already set in the environment).

    No-op on missing file; does not raise. Logs a debug line on success and
    a warning if the file is present but unreadable.
    """
    root = repo_root or Path.cwd()
    env_path = root / ".env"
    if not env_path.exists():
        return 0

    try:
        from dotenv import dotenv_values
    except ImportError:
        logger.warning(
            "python-dotenv not installed; cannot auto-load %s. "
            "Install with `pip install python-dotenv`.",
            env_path,
        )
        return 0

    try:
        values = dotenv_values(env_path)
    except OSError as exc:
        logger.warning("Failed to read %s: %s", env_path, exc)
        return 0

    loaded = 0
    for key, value in values.items():
        if value is None or key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1
    if loaded:
        logger.debug("Loaded %d vars from %s", loaded, env_path)
    return loaded


def launch_detached(repo_root: Path | None = None) -> int:
    """Spawn the watcher daemon as a fully detached subprocess.

    The child inherits the parent's environment (which by the time this is
    called includes any keys loaded from .env). stdout + stderr are
    redirected to ``.claude/watcher.log`` (appended); stdin is /dev/null.

    Returns the child PID. Parent should print + exit immediately; the
    child writes its own ``.claude/watcher.pid`` on startup via the
    existing watcher lifecycle.

    The child invocation is `python -m app.cli watcher` without `--detach`,
    so the child runs the foreground watcher path in its detached process.
    """
    root = repo_root or Path.cwd()
    claude_dir = root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    log_path = claude_dir / "watcher.log"

    cmd = [sys.executable, "-m", "app.cli", "watcher"]

    # Open the log file once; child inherits the fd. We close our handle
    # after Popen returns — the child keeps its own copy alive.
    log_file = log_path.open("ab")
    try:
        if sys.platform == "win32":
            creationflags = (
                _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
            )
            proc = subprocess.Popen(  # nosec B603
                cmd,
                cwd=str(root),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=False,  # required when redirecting stdio on Windows
            )
        else:
            proc = subprocess.Popen(  # nosec B603
                cmd,
                cwd=str(root),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    finally:
        log_file.close()
    return proc.pid


def launch_in_new_terminal(repo_root: Path | None = None) -> int:
    """Windows: open a new cmd.exe window with the watcher running attached.

    The new window's title is "watcher"; the watcher runs in the foreground
    inside it so the operator can see live logs. `.env` is auto-loaded by
    the child's own `_run_watcher` call.

    Returns 0 on success, 1 on non-Windows (with a clear stderr message).
    Does not wait for the spawned window.
    """
    if sys.platform != "win32":
        print(
            "Error: --visible is only supported on Windows. "
            "Use --detach for cross-platform background launch.",
            file=sys.stderr,
        )
        return 1

    root = repo_root or Path.cwd()
    # `start "watcher" cmd /k "cd /d <root> && python -m app.cli watcher"`
    inner = f'cd /d "{root}" && "{sys.executable}" -m app.cli watcher'
    subprocess.run(  # nosec B603 B607
        ["cmd.exe", "/c", "start", "watcher", "cmd", "/k", inner],
        check=False,
    )
    return 0
