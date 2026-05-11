"""PostToolUse hook: invoke check_patch_paths.py after git mv / git rename.

Guarded by the Bash discriminator inside the hook — the PostToolUse "Bash"
matcher fires for *every* Bash call, but this script only triggers the check
when the command contains ``git mv`` or ``git rename`` so workers see stale
patch strings within seconds of the rename.

Output is **informational** (never exits non-zero) so it never blocks the
tool-use flow.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _SCRIPT_DIR.parent / "scripts"
_RENAME_RE = re.compile(r"\bgit\s+mv\b|\bgit\s+rename\b")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # fail-open

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd.strip():
        return 0

    # Only fire on rename-shaped commands — not on every Bash invocation.
    if not _RENAME_RE.search(cmd):
        return 0

    check_script = _SCRIPTS_DIR / "check_patch_paths.py"
    if not check_script.is_file():
        return 0  # graceful skip if script is missing

    try:
        proc = subprocess.run(  # nosec B603
            [sys.executable, str(check_script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return 0  # fail-open on invocation errors

    # Always exit 0 — informational only.
    if proc.stdout:
        print(proc.stdout, end="", flush=True)
    if proc.stderr and proc.returncode != 0:
        print(proc.stderr, end="", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
