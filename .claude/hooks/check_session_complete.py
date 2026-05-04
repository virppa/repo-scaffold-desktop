"""Stop hook — enforce worker-session completion contract (WOR-372).

Fires when Claude Code attempts to end the session. For worker sessions
(those running in a linked git worktree with an execution manifest), this
hook enforces two invariants:

1. ``result.json`` must exist at the manifest's ``artifact_paths.result_json``.
2. ``git status --porcelain`` must be clean — i.e., the worker's changes
   were committed.

If either gate fails, the hook emits a JSON ``{"decision": "block",
"reason": "..."}`` so Claude Code re-prompts the model to execute the
missing actions. If the hook has already blocked once in this session
(``stop_hook_active`` is true), the gate is skipped to prevent infinite
loops.

Operator sessions running in the main repo (not a linked worktree) pass
through unconditionally — the hook only enforces in worker contexts.

Wire in ``.claude/settings.json``::

    "hooks": {
      "Stop": [
        {
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/check_session_complete.py"
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from pathlib import Path


def _is_linked_worktree(cwd: Path) -> bool:
    """Return True if cwd is inside a linked git worktree (not the main one).

    The watcher creates worker sessions inside ``worktrees/<branch-slug>/``,
    which are linked worktrees: their ``.git`` is a file pointing at the
    main repo's ``.git/worktrees/<name>/`` directory. Operator sessions run
    in the main worktree where ``.git`` is a directory.

    The cleanest check: ``git rev-parse --git-dir`` and ``--git-common-dir``
    return the same path in the main worktree, different paths in linked
    worktrees.
    """
    try:
        git_dir = subprocess.check_output(  # nosec B603 B607
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=str(cwd),
            text=True,
            timeout=5,
        ).strip()
        common_dir = subprocess.check_output(  # nosec B603 B607
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(cwd),
            text=True,
            timeout=5,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return Path(git_dir).resolve() != Path(common_dir).resolve()


def _find_manifest(cwd: Path) -> Path | None:
    """Return the manifest path for the active worker session, or None."""
    candidates = sorted(
        cwd.glob(".claude/artifacts/*/manifest.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _block(reason: str) -> int:
    """Print the block payload to stdout and return 0 (Claude Code reads stdout)."""
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # Malformed input — fail open

    # Anti-loop: Claude Code sets stop_hook_active=True when it has already
    # been blocked once. Don't block a second time.
    if payload.get("stop_hook_active"):
        return 0

    cwd = Path(payload.get("cwd", ".")).resolve()

    # Operator sessions in the main worktree pass through.
    if not _is_linked_worktree(cwd):
        return 0

    manifest_path = _find_manifest(cwd)
    if manifest_path is None:
        return 0  # Linked worktree but no manifest — not enforceable

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0  # Unreadable manifest — fail open

    ticket_id = manifest.get("ticket_id", "WOR-NNN")

    # Gate 1: result.json must exist.
    result_json_rel = manifest.get("artifact_paths", {}).get("result_json")
    if result_json_rel:
        result_json_path = cwd / result_json_rel
        if not result_json_path.exists():
            return _block(
                f"Session cannot end — {result_json_rel} is missing. "
                "Use the Write tool to create it now with these keys: "
                'ticket_id, status ("success" or "failure"), summary, '
                "checks_passed (list of check commands that passed), "
                "checks_failed (list of failures, empty if status=success). "
                "Do not run any further verification commands — your work "
                "is verified by the checks already passed; the watcher only "
                "needs the artifact."
            )

    # Gate 2: working tree must be clean.
    try:
        out = subprocess.check_output(  # nosec B603 B607
            ["git", "status", "--porcelain"],
            cwd=str(cwd),
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return 0  # Git unavailable — fail open

    if out.strip():
        return _block(
            "Session cannot end — uncommitted changes:\n"
            f"{out}\n"
            f"Run this exact command now: "
            f"git add -A && git commit -m 'Part of {ticket_id}: <one-line summary>'"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
