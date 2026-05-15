"""PostToolUse hook to flag 5+ edits to the same file within 5 minutes.

Detects the WOR-401-class mock-path thrash pattern — an operator editing
the same file repeatedly in one session, typically chasing a mock path
that keeps breaking.  Five edits in five minutes is treated as a signal
to pause and follow the CLAUDE.md mock-path migration rules.

State file: ``<repo>/.claude/.thrash_state.json``.  Keyed by
``session_id`` so a stale file from a prior session does not leak into
the current one.  Persisted before any potential print so the count is
always durable.

The state file is anchored to the hook script's location (``__file__``),
not the cwd from the payload.

Wire in ``.claude/settings.json``::

    "hooks": {
      "PostToolUse": [
        {
          "matcher": "Edit|Write",
          "hooks": [
            {
              "type": "command",
              "command": "python .claude/hooks/posttooluse_thrash_detector.py"
            }
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

EDITS_CAP = 5
WINDOW_SECONDS = 300  # 5 minutes

STATE_FILENAME = ".thrash_state.json"


def _warn(message: str) -> int:
    print(json.dumps({"thrash_warning": message}))
    return 0


def _prune_timestamps(timestamps: list[float], now: float) -> list[float]:
    cutoff = now - WINDOW_SECONDS
    return [t for t in timestamps if t > cutoff]


def main(state_path: Path | None = None) -> int:
    print("posttooluse_thrash_detector: fired", file=sys.stderr, flush=True)
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    if not isinstance(payload, dict):
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        return 0

    # Both Edit and Write pass the target path under file_path.
    file_path_raw = payload.get("tool_input", {}).get("file_path")
    if not file_path_raw:
        return 0

    session_id = payload.get("session_id", "")

    # State file lives at <repo>/.claude/.thrash_state.json.
    # Anchor to the hook script's own location, not the cwd from the
    # payload.
    if state_path is None:
        hook_dir = Path(__file__).resolve().parent  # <repo>/.claude/hooks
        state_path = hook_dir.parent / STATE_FILENAME

    # Normalize the file path so different relative spellings of the same
    # file collapse to one key.
    try:
        candidate = Path(file_path_raw)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        normalized = str(candidate.resolve())
    except (OSError, RuntimeError):
        normalized = file_path_raw

    # Load existing state; reset if session_id changed (stale from prior run).
    # Always build a fresh state dict so edits don't leak across sessions.
    edits: dict[str, list[float]] = {}
    state: dict[str, object] = {"session_id": session_id, "edits": edits}
    if state_path.exists():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("session_id") == session_id:
                raw_edits = existing.get("edits") or {}
                if isinstance(raw_edits, dict):
                    # Copy edits so mutations don't affect the raw loaded dict.
                    for k, v in raw_edits.items():
                        if isinstance(v, list):
                            edits[k] = v[:]
                        else:
                            edits[k] = []
                state = {"session_id": session_id, "edits": edits}
        except (OSError, json.JSONDecodeError):
            pass

    now = time.time()

    # Initialize timestamps list for this file if needed.
    if normalized not in edits:
        edits[normalized] = []

    timestamps = edits[normalized]
    if not isinstance(timestamps, list):
        timestamps = []
        edits[normalized] = timestamps

    # Prune stale timestamps outside the rolling window.
    timestamps[:] = _prune_timestamps(timestamps, now)

    # Record this edit.
    timestamps.append(now)

    state["session_id"] = session_id

    # Persist BEFORE deciding — if write fails, fail open (don't warn
    # on infra issues).
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        return 0

    # Count is the number of edits within the window.
    count = len(timestamps)

    if count >= EDITS_CAP:
        elapsed_minutes = (
            (timestamps[-1] - timestamps[0]) / 60.0 if len(timestamps) > 1 else 0
        )
        leaf = Path(file_path_raw).name
        message = (
            f"{count}th edit to {leaf} in {elapsed_minutes:.0f} minutes "
            f"— iterating on test mocks? "
            f"See CLAUDE.md mock-path migration rules "
            f"(anchor: mock-path migration section)"
        )
        return _warn(message)

    return 0


if __name__ == "__main__":
    sys.exit(main())
