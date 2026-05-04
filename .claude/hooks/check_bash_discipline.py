"""PreToolUse hook on Bash — enforce three Worker Efficiency rules (WOR-376).

Each rule is documented as prose in CLAUDE.md "Worker efficiency"; this hook
makes them deterministic. Three checks, each with its own disposition:

1. **Bare ``cd <path>``** (regex anchors to a complete command with no
   chaining). Disposition: warn-don't-block — wasteful round-trip but not
   destructive.

2. **Heredocs writing source files** (``cat > file.py <<`` / ``tee file.py <<``
   with extensions matching .py/.md/.json/.yaml/.yml/.toml).
   Disposition: block — heredocs break on Windows when the body has single
   quotes; the Write tool handles any content without escaping.

3. **`python3 -c` opening files** (any inline `-c` script that includes
   ``open(...)``). Disposition: block — Python source through a shell
   command breaks on Windows quoting, and editing files via `python -c`
   bypasses the Edit tool's diff confirmation.

Wire in ``.claude/settings.json``::

    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Bash",
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/check_bash_discipline.py"
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
import re
import sys

# Bare `cd <path>` — no `&&`, `;`, `|`, redirects, or backgrounding after the path.
# Matches: "cd /path", "  cd ~/foo  "
# Does NOT match: "cd /path && ls", "cd /path; ls", "cd /path | tee", "cd /path > log"
_BARE_CD = re.compile(r"^\s*cd\s+\S+\s*$")

# Heredoc writing a source file. Two shapes:
#   `cat > file.ext <<EOF`  / `cat >> file.ext << 'EOF'`
#   `tee file.ext <<EOF`    / `tee -a file.ext <<EOF`   (tee writes from stdin)
# Discriminating feature: `cat`/`tee`, a source-extension path, and `<<` somewhere
# downstream. The exact intervening syntax varies; match permissively.
_HEREDOC_TO_SOURCE = re.compile(
    r"\b(cat|tee)\b[^\n]*?\S+\.(py|md|json|yaml|yml|toml)\s*<<",
    re.IGNORECASE,
)

# `python -c` or `python3 -c` containing an `open(` call (read or write — both
# are anti-patterns; reads should use the Read tool, writes should use Write/Edit).
_PYTHON_DASH_C_OPEN = re.compile(
    r"\bpython3?\s+-c\s+.*\bopen\s*\(",
    re.DOTALL,
)


def _block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def _warn(reason: str) -> int:
    """Emit a warning to stderr without blocking. Claude Code surfaces stderr
    in the tool result so the model sees the advisory but the call proceeds."""
    print(reason, file=sys.stderr)
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # Fail open

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = payload.get("tool_input", {}).get("command", "")
    if not isinstance(cmd, str) or not cmd.strip():
        return 0

    # Rule 2: heredoc-to-source-file (highest blast radius — broken file content)
    if _HEREDOC_TO_SOURCE.search(cmd):
        return _block(
            "Bash blocked: heredoc writing a source file. Heredocs break on "
            "Windows when the body contains single quotes (the shell "
            "misinterprets them as closing the delimiter), and on POSIX "
            "they require escaping that the Write tool handles natively. "
            "Use the Write tool instead — it takes the file content as a "
            "string with no shell quoting involved. (CLAUDE.md 'Worker "
            "efficiency')"
        )

    # Rule 3: python -c opening files
    if _PYTHON_DASH_C_OPEN.search(cmd):
        return _block(
            "Bash blocked: `python -c` calling `open(...)`. Python source "
            "passed through a shell command breaks on Windows quoting and "
            "loses Edit's diff confirmation. Use the Read tool to read a "
            "file; use Edit (with old_string/new_string) to modify; use "
            "Write to create. (CLAUDE.md 'Worker efficiency')"
        )

    # Rule 1: bare `cd` — warn but allow, since some tools still need it
    # (e.g., one-off interactive ops) and false positives are higher.
    if _BARE_CD.match(cmd):
        return _warn(
            "[bash-discipline] Standalone `cd` is a wasted round-trip. "
            "Chain with the actual command: `cd /path && <cmd>`, or pass "
            "an absolute path to the next tool call."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
