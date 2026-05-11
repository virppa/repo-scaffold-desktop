"""PreToolUse hook — block worker-side PR/push operations (WOR-444).

Workers must not open PRs or push to origin themselves: those are the
watcher's job, and a worker-side `gh pr create` collides with the
watcher's later `attempt_pr` step, marking the ticket Blocked even
though the PR is open and ready (the WOR-67 incident on 2026-05-11).

This hook blocks the following commands when invoked from a watcher-
spawned worker session (detected via `WATCHER_WORKER=1`):

  - `gh pr create` (any variant)
  - `gh pr edit` (any variant — prevent worker from touching PR metadata)
  - `gh pr merge` (any variant — only the watcher's auto-merge should merge)
  - `git push` to origin (worker should commit locally; watcher pushes)

Operator-invoked sessions (no `WATCHER_WORKER` env var) bypass this hook,
so `/finalize-ticket` works manually.

Fails open on any JSON/IO error — never want this hook to surprise-break
a session.
"""

from __future__ import annotations

import json
import os
import re
import sys

# `gh pr <action>` patterns to block. Match the start of the command after
# lstrip so chained/wrapped invocations are also caught.
_GH_PR_BLOCKED = re.compile(r"^gh\s+pr\s+(create|edit|merge)\b")

# `git push` to the origin remote. Matches `git push`, `git push origin`,
# `git push -u origin <ref>`, etc. Does NOT match local-only operations
# like `git push gh-pages-mirror` (different remote) or `git fetch origin`.
# A bare `git push` resolves to the configured upstream (origin/<branch>)
# in the watcher's worktree setup, so block bare pushes too.
_GIT_PUSH_ORIGIN = re.compile(r"^git\s+push\b(?:\s+-[^\s]+)*(?:\s+origin\b|\s*$)")


def _block(reason: str) -> int:
    """Emit a structured block decision in the format Claude Code understands."""
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main() -> int:
    if os.environ.get("WATCHER_WORKER") != "1":
        return 0  # operator session — let everything through

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # fail open

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = payload.get("tool_input", {}).get("command", "")
    if not isinstance(cmd, str):
        return 0
    stripped = cmd.lstrip()

    if _GH_PR_BLOCKED.match(stripped):
        return _block(
            "Hook-trust violation (WOR-444): `gh pr create/edit/merge` is "
            "blocked in worker sessions. The watcher opens, edits, and "
            "merges PRs at finalize time — calling it from inside the "
            "worker creates a duplicate PR or races the state machine "
            "(see WOR-67 incident 2026-05-11). Just commit your work and "
            "exit; the watcher handles the rest. See "
            ".claude/commands/implement-ticket.md step 6."
        )

    if _GIT_PUSH_ORIGIN.match(stripped):
        return _block(
            "Hook-trust violation (WOR-444): `git push` to origin is "
            "blocked in worker sessions. The watcher pushes the worker "
            "branch at finalize time (after rebasing onto the latest "
            "base). Pushing from the worker bypasses that rebase and "
            "can cause merge conflicts on the watcher's later PR step. "
            "Just commit locally; the watcher will push for you."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
