#!/usr/bin/env python3
"""Register the WOR-477 / WOR-483 PostToolUse hooks in .claude/settings.json.

Why a script: `.claude/settings.json` is PreToolUse-blocked for the Claude
Code Edit/Write tools (and is in every manifest's forbidden_paths), so no
automated session can wire these hooks in — that is exactly why WOR-494
exists. This script is run by a human operator as a normal `python`
process (not via the Edit/Write tools), so it is NOT subject to that
block.

Bakes WOR-494 into WOR-493: run this **on the epic branch**
(`epic/wor-493-tier-12-local-batch-1`) so the settings.json change ships
inside the single human-reviewed epic→main PR, atomically with the hook
scripts themselves — instead of leaving the hooks as inert dead code.

    git checkout epic/wor-493-tier-12-local-batch-1
    python scripts/register_posttooluse_hooks.py
    git diff .claude/settings.json          # review
    # smoke-test each hook, then:
    git add .claude/settings.json scripts/register_posttooluse_hooks.py
    git commit -m "WOR-494 Register WOR-477/WOR-483 PostToolUse hooks (Closes WOR-494)"
    git push

Idempotent: an entry whose command is already present under PostToolUse
is skipped, so re-running (or running after a partial apply) is safe.
Matchers/commands are taken verbatim from each hook module's own
docstring (the authors' intended registration).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / ".claude" / "settings.json"

# Verbatim from the hooks' module docstrings on the epic branch.
ENTRIES: list[dict] = [
    {
        "matcher": "Read|Bash|Edit|Write",
        "hooks": [
            {
                "type": "command",
                "command": "python .claude/hooks/posttooluse_parallel_nudge.py",
            }
        ],
    },
    {
        "matcher": "Edit|Write",
        "hooks": [
            {
                "type": "command",
                "command": "python .claude/hooks/posttooluse_thrash_detector.py",
            }
        ],
    },
]


def _commands(entry: dict) -> set[str]:
    return {h.get("command", "") for h in entry.get("hooks", []) if isinstance(h, dict)}


def main() -> int:
    if not SETTINGS.exists():
        print(
            f"ERROR: {SETTINGS} not found. Run on the epic branch checkout.",
            file=sys.stderr,
        )
        return 1

    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    hooks = data.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    if not isinstance(post, list):
        print("ERROR: hooks.PostToolUse is not a list — aborting.", file=sys.stderr)
        return 1

    already: set[str] = set()
    for e in post:
        if isinstance(e, dict):
            already |= _commands(e)

    added: list[str] = []
    for entry in ENTRIES:
        cmd = next(iter(_commands(entry)))
        if cmd in already:
            print(f"skip  (already registered): {cmd}")
            continue
        post.append(entry)
        added.append(cmd)
        print(f"add   matcher={entry['matcher']!r}: {cmd}")

    if not added:
        print("\nNothing to do — both hooks already registered.")
        return 0

    SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    n = len(added)
    print(f"\nUpdated {SETTINGS} (+{n} PostToolUse entr{'y' if n == 1 else 'ies'}).")
    print(
        "NOTE: the whole file was JSON round-tripped (indent=2). Review the\n"
        "diff — semantics are unchanged but whitespace/escaping may normalize.\n"
        "Then smoke-test each hook and commit on the epic branch (WOR-494)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
