"""Measure parallel tool_use adoption rate across worker JSONL logs.

A worker session can emit one tool_use block per assistant message (serial)
or multiple tool_use blocks in the same message (parallel — see WOR-387).
The two patterns produce identical end results but very different wall times:
parallel collapses N tool round-trips into one turn boundary, which matters
because each round-trip costs prefill + decode warmup (~10-30s on long
context).

CLAUDE.md "Worker efficiency" tells workers to emit parallel tool calls
when calls are independent. This script measures whether they actually do.

Usage (from repo root):

    python scripts/metrics_analysis/measure_parallel_tool_use.py
        # walks .claude/artifacts/wor_*/worker_*.log

    python scripts/metrics_analysis/measure_parallel_tool_use.py --epic WOR-394
        # only sessions whose sibling manifest.json names this epic_id

    python scripts/metrics_analysis/measure_parallel_tool_use.py \
        --logs path/to/specific.log path/to/another.log

Output is a per-session table on stdout, then a per-epic aggregate. Counts:
  total_tu_msgs      — assistant messages with ≥1 tool_use block
  parallel_tu_msgs   — assistant messages with ≥2 tool_use blocks
  parallel_rate      — parallel / total
  total_tool_calls   — sum of all tool_use blocks across all assistant msgs

Tool-pair breakdown shows the most common parallel combinations.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ARTIFACTS_DIR = _REPO_ROOT / ".claude" / "artifacts"


@dataclass
class SessionStats:
    ticket_id: str
    epic_id: str | None
    log_path: Path
    total_assistant_msgs: int = 0
    total_tu_msgs: int = 0
    parallel_tu_msgs: int = 0
    total_tool_calls: int = 0
    pair_counts: Counter[tuple[str, ...]] = field(default_factory=Counter)
    tool_counts: Counter[str] = field(default_factory=Counter)

    @property
    def parallel_rate(self) -> float:
        if self.total_tu_msgs == 0:
            return 0.0
        return self.parallel_tu_msgs / self.total_tu_msgs


def _read_epic_id(artifact_dir: Path) -> str | None:
    """Return the epic_id from sibling manifest.json, or None if not present."""
    manifest_path = artifact_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    epic_id = data.get("epic_id")
    return str(epic_id) if epic_id else None


def _ticket_id_from_log(log_path: Path) -> str:
    """Derive a WOR-NNN style id from the log filename ``worker_wor-NN.log``."""
    stem = log_path.stem  # e.g. "worker_wor-407"
    if stem.startswith("worker_"):
        return stem[len("worker_") :].upper().replace("_", "-")
    return stem.upper()


def measure_session(log_path: Path) -> SessionStats:
    """Walk the JSONL log and accumulate per-session counts.

    Groups content blocks by ``message.id`` because Claude Code's stream-json
    format emits each content block as a separate JSONL event sharing the
    same parent message id. A single logical assistant response containing
    [thinking, tool_use, tool_use] appears as three separate ``type:assistant``
    events in the log; counting events instead of message-ids would
    miscount every multi-block response as multiple single-block messages
    (memory: ``reference_jsonl_multi_tool_measurement.md``).
    """
    artifact_dir = log_path.parent
    epic_id = _read_epic_id(artifact_dir)
    stats = SessionStats(
        ticket_id=_ticket_id_from_log(log_path),
        epic_id=epic_id,
        log_path=log_path,
    )

    blocks_by_msg_id: dict[str, list[dict[str, object]]] = {}
    with log_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            msg_id = msg.get("id")
            if not msg_id:
                continue
            content = msg.get("content") or []
            if not isinstance(content, list):
                continue
            blocks_by_msg_id.setdefault(msg_id, []).extend(
                b for b in content if isinstance(b, dict)
            )

    stats.total_assistant_msgs = len(blocks_by_msg_id)
    for blocks in blocks_by_msg_id.values():
        tool_names_in_msg: list[str] = []
        for b in blocks:
            if b.get("type") != "tool_use":
                continue
            name = b.get("name")
            tool_names_in_msg.append(name if isinstance(name, str) else "<unknown>")
        if not tool_names_in_msg:
            continue
        stats.total_tu_msgs += 1
        stats.total_tool_calls += len(tool_names_in_msg)
        for name in tool_names_in_msg:
            stats.tool_counts[name] += 1
        if len(tool_names_in_msg) >= 2:
            stats.parallel_tu_msgs += 1
            stats.pair_counts[tuple(sorted(tool_names_in_msg))] += 1
    return stats


def _gather_log_paths(artifacts_root: Path, explicit: list[Path] | None) -> list[Path]:
    if explicit:
        return [p for p in explicit if p.exists()]
    return sorted(artifacts_root.glob("wor_*/worker_*.log"))


def _format_session_row(s: SessionStats) -> str:
    rate_pct = f"{s.parallel_rate * 100:.1f}%"
    epic = s.epic_id or "-"
    return (
        f"{s.ticket_id:<10} {epic:<12} "
        f"{s.total_tu_msgs:>6} {s.parallel_tu_msgs:>6} "
        f"{rate_pct:>9} {s.total_tool_calls:>6}"
    )


def _print_session_table(sessions: list[SessionStats]) -> None:
    print(
        f"{'ticket':<10} {'epic':<12} {'tu_msgs':>6} {'par_msg':>6} "
        f"{'par_rate':>9} {'tools':>6}"
    )
    print("-" * 60)
    for s in sessions:
        print(_format_session_row(s))


def _print_epic_aggregates(sessions: list[SessionStats]) -> None:
    by_epic: dict[str, list[SessionStats]] = {}
    for s in sessions:
        key = s.epic_id or "<no-epic>"
        by_epic.setdefault(key, []).append(s)
    if len(by_epic) <= 1:
        return
    print()
    print("By epic:")
    print(f"{'epic':<14} {'tickets':>7} {'tu_msgs':>7} {'par_msg':>7} {'par_rate':>9}")
    print("-" * 50)
    for epic_id in sorted(by_epic):
        group = by_epic[epic_id]
        tu = sum(s.total_tu_msgs for s in group)
        par = sum(s.parallel_tu_msgs for s in group)
        rate = par / tu if tu else 0.0
        print(f"{epic_id:<14} {len(group):>7} {tu:>7} {par:>7} {rate * 100:>8.1f}%")


def _print_top_pairs(sessions: list[SessionStats], limit: int = 12) -> None:
    combined: Counter[tuple[str, ...]] = Counter()
    for s in sessions:
        combined.update(s.pair_counts)
    if not combined:
        return
    print()
    print(f"Top {limit} parallel tool combinations across all sessions:")
    for combo, count in combined.most_common(limit):
        combo_str = " + ".join(combo)
        print(f"  {count:>5}  {combo_str}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--epic",
        help="Filter to sessions whose sibling manifest.json names this epic_id",
    )
    parser.add_argument(
        "--logs",
        nargs="+",
        type=Path,
        help="Specific log paths instead of globbing artifacts/",
    )
    args = parser.parse_args()

    log_paths = _gather_log_paths(_ARTIFACTS_DIR, args.logs)
    if not log_paths:
        print(f"No worker logs found under {_ARTIFACTS_DIR}")
        return 1

    sessions = [measure_session(p) for p in log_paths]
    if args.epic:
        sessions = [s for s in sessions if s.epic_id == args.epic]
        if not sessions:
            print(f"No sessions match epic={args.epic}")
            return 1

    sessions.sort(key=lambda s: (s.epic_id or "", s.ticket_id))
    _print_session_table(sessions)
    _print_epic_aggregates(sessions)
    _print_top_pairs(sessions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
