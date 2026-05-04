"""PreToolUse hook on Read — enforce the per-file 2-read cap (WOR-355, WOR-371).

Fires before every Read tool call. Tracks per-file read counts in a
session-scoped state file, and emits a ``{"decision": "block"}`` payload
when a file is read more than ``CAP`` times in a single session.

The 2-read cap was established in WOR-355: a file may be read at most
twice per session, regardless of whether ``context_snippets`` was populated
in the manifest. Re-reads are most often the "verify after edit"
anti-pattern — the Edit tool result is authoritative; trusting it saves
a round-trip.

State file: ``<cwd>/.claude/.read_counts.json``. Keyed by ``session_id`` so
a stale file from a prior session does not leak into the current one.
Persisted before any potential block so the count is always durable.

Wire in ``.claude/settings.json``::

    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Read",
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/check_read_cap.py"
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CAP = 2
STATE_FILENAME = ".claude/.read_counts.json"


def _block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    if payload.get("tool_name") != "Read":
        return 0

    file_path_raw = payload.get("tool_input", {}).get("file_path")
    if not file_path_raw:
        return 0

    cwd = Path(payload.get("cwd", ".")).resolve()
    session_id = payload.get("session_id", "")
    state_path = cwd / STATE_FILENAME

    # Normalize the file path so different relative spellings of the same
    # file collapse to one key. Resolve against the payload's cwd, not the
    # hook process's — Claude Code passes absolute paths in practice, but
    # relative ones must anchor to the worker's cwd to be meaningful.
    try:
        candidate = Path(file_path_raw)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        normalized = str(candidate.resolve())
    except (OSError, RuntimeError):
        normalized = file_path_raw

    # Load existing state; reset if session_id changed (stale from prior run).
    state: dict[str, object] = {"session_id": session_id, "counts": {}}
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("session_id") == session_id:
                state = existing
        except (OSError, json.JSONDecodeError):
            pass

    counts = state.setdefault("counts", {})
    if not isinstance(counts, dict):
        counts = {}
        state["counts"] = counts

    new_count = int(counts.get(normalized, 0)) + 1
    counts[normalized] = new_count
    state["session_id"] = session_id

    # Persist before deciding. If write fails, fail open (don't block on infra issues).
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        return 0

    if new_count > CAP:
        leaf = Path(file_path_raw).name
        return _block(
            f"Read blocked: {leaf} has already been read {new_count - 1} times "
            f"this session. The per-file {CAP}-read cap (WOR-355) prevents "
            "context bloat. If you need a different section of the file, use "
            "Grep with a narrow pattern instead. If you are trying to verify "
            "that an Edit landed, do not — the Edit tool result already shows "
            "the exact diff applied; that result is authoritative."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
