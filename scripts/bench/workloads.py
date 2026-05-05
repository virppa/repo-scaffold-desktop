"""Benchmark workload definitions.

A *workload* is a self-contained multi-turn session (e.g. a real worker session
shape) that the benchmark runner executes against a backend.  This module is
additive -- it does not replace existing ``--tier`` execution paths.

Usage::

    python scripts/bench/run_bench.py --workload watcher-pattern \\
        --config config/bench-watcher-pattern.toml \\
        --backend local_vllm --model qwen3-coder
"""

from __future__ import annotations

import json
import logging
import re
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PhaseConfig:
    """Configuration for one phase of a workload session."""

    name: str
    n_turns: int
    message_template: str
    task_template: str = ""
    tool_result_size_base: int = 0
    tool_result_size_growth: int = 0
    post_context_template: str = ""
    tool_result_is_summary: bool = False
    tool_result_size: int = 0


@dataclass
class SessionConfig:
    """Complete configuration for a workload session."""

    n_turns: int
    compaction_turn: int
    phases: list[PhaseConfig]
    max_tool_result_size: int


@dataclass
class TurnResult:
    """Timing and metric result for a single turn."""

    turn_index: int
    phase_name: str
    phase_index: int
    message_size_chars: int
    tool_result_size_chars: int
    ttft_s: float | None = None
    total_s: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


@dataclass
class WorkloadResult:
    """Full session result."""

    n_turns: int
    total_seconds: float
    phases: list[PhaseResult] = field(default_factory=list)
    accumulated_findings: list[str] = field(default_factory=list)
    compaction: dict[str, Any] | None = None
    any_failure: bool = False
    avg_tool_result_size: float | None = None
    summary_message_size: int | None = None


@dataclass
class PhaseResult:
    """Timing result for one phase of the session."""

    name: str
    n_turns: int
    prefill_avg_s: float | None = None
    prefill_min_s: float | None = None
    prefill_max_s: float | None = None
    total_seconds: float | None = None
    avg_input_tokens: float | None = None
    avg_output_tokens: float | None = None
    avg_tool_result_size: float | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class Workload(Protocol):
    """A benchmark workload that runs a multi-turn session."""

    def load_config(self, config_path: str | Path) -> SessionConfig: ...

    def generate_turns(
        self, session_config: SessionConfig
    ) -> tuple[list[TurnResult], dict[str, Any] | None]:
        """Generate turns for one session run.

        Returns
        -------
        turns : list[TurnResult]
            One entry per phase (one entry in the compact phase, one per turn
            in pre-compact and post-compact).
        compaction : dict or None
            Compaction metadata (raw_count, summary_size) if there is a
            compact phase, otherwise ``None``.
        """


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_session_config(path: str | Path) -> SessionConfig:
    """Parse bench-watcher-pattern.toml into a SessionConfig."""
    raw = Path(path).read_bytes()
    data = tomllib.loads(raw.decode())

    sess = data.get("session", {})
    n_turns = int(sess.get("n_turns", 100))
    compaction_turn = int(sess.get("compaction_turn", 60))
    max_tool_result_size = int(sess.get("max_tool_result_size", 800))

    phases_raw = data.get("phases", [])
    phases: list[PhaseConfig] = []
    for p in phases_raw:
        phases.append(
            PhaseConfig(
                name=p["name"],
                n_turns=int(p["n_turns"]),
                message_template=str(p.get("message_template", "")),
                task_template=str(p.get("task_template", "")),
                tool_result_size_base=int(p.get("tool_result_size_base", 0)),
                tool_result_size_growth=int(p.get("tool_result_size_growth", 0)),
                post_context_template=str(p.get("post_context_template", "")),
                tool_result_is_summary=bool(p.get("tool_result_is_summary", False)),
                tool_result_size=int(p.get("tool_result_size", 0)),
            )
        )

    return SessionConfig(
        n_turns=n_turns,
        compaction_turn=compaction_turn,
        phases=phases,
        max_tool_result_size=max_tool_result_size,
    )


# ---------------------------------------------------------------------------
# WatcherPatternWorkload
# ---------------------------------------------------------------------------


class WatcherPatternWorkload(Workload):
    """Simulates a real worker session: tool_result accumulation + compaction.

    Session shape
    -------------
    1. **Pre-compact** (turns 0..N-1): each turn appends a new tool_result
       that grows linearly in size.  The messages list grows cumulatively.
    2. **Compact** (turn N): the assistant summarizes accumulated findings.
    3. **Post-compact** (turns N+1..end): new turns continue with the
       compacted summary replacing raw accumulation.
    """

    def load_config(self, config_path: str | Path) -> SessionConfig:
        return _load_session_config(config_path)

    def generate_turns(
        self, session_config: SessionConfig
    ) -> tuple[list[TurnResult], dict[str, Any] | None]:
        """Generate turns across all phases."""

        accumulated: list[str] = []
        turns: list[TurnResult] = []

        for phase in session_config.phases:
            if phase.name == "pre-compact":
                phase_turns, accumulated = self._run_pre_compact(
                    phase, session_config, accumulated
                )
            elif phase.name == "compact":
                phase_turns, summary, raw_count = self._run_compact(
                    phase, session_config, accumulated
                )
                turns.extend(phase_turns)
                compaction = {
                    "raw_count": raw_count,
                    "summary_size": len(summary),
                }
                continue
            else:  # post-compact
                phase_turns, accumulated = self._run_post_compact(
                    phase, session_config, accumulated
                )
            turns.extend(phase_turns)

        return turns, compaction

    # ── Pre-compact phase ─────────────────────────────────────────────────

    def _run_pre_compact(
        self,
        phase: PhaseConfig,
        cfg: SessionConfig,
        accumulated: list[str],
    ) -> tuple[list[TurnResult], list[str]]:
        results: list[TurnResult] = []

        for turn_idx in range(phase.n_turns):
            # Linear growth: base at turn 0, max at last turn
            growth = (
                (phase.tool_result_size_growth * turn_idx) if phase.n_turns > 1 else 0
            )
            max_growth = (
                phase.tool_result_size_growth * (phase.n_turns - 1)
                if phase.n_turns > 1
                else 1
            )
            if max_growth > 0:
                tool_size = int(
                    phase.tool_result_size_base
                    + (growth / max_growth)
                    * (cfg.max_tool_result_size - phase.tool_result_size_base)
                )
            else:
                tool_size = phase.tool_result_size_base

            # Build user message with accumulated context
            ctx_parts = []
            for i, tr in enumerate(accumulated[-8:], 1):
                ctx_parts.append(f"[Finding {i}] {tr[:200]}")

            ctx_hint = ""
            if ctx_parts:
                ctx_hint = "\n\nContext from previous turns:\n" + "\n".join(ctx_parts)

            task_idx = turn_idx + 1
            task_label = phase.task_template.format(idx=task_idx)
            header = phase.message_template.format(turn=turn_idx + 1, task=task_label)
            user_msg = {
                "role": "user",
                "content": (f"{header}\n{ctx_hint}"),
            }

            tool_text = self._format_tool_result(turn_idx, task_idx, tool_size)

            results.append(
                TurnResult(
                    turn_index=turn_idx,
                    phase_name=phase.name,
                    phase_index=turn_idx,
                    message_size_chars=len(json.dumps(user_msg)),
                    tool_result_size_chars=tool_size,
                )
            )
            accumulated.append(tool_text)

        return results, accumulated

    # ── Compact phase ─────────────────────────────────────────────────────

    def _run_compact(
        self,
        phase: PhaseConfig,
        cfg: SessionConfig,
        accumulated: list[str],
    ) -> tuple[list[TurnResult], str, int]:
        raw_count = len(accumulated)

        if raw_count == 0:
            summary = "No findings to compact."
        else:
            findings = list(accumulated)
            summary = f"Compacted summary of {raw_count} findings:\n" + "\n".join(
                f"  - {tr.split(chr(10))[0][:120]}" for tr in findings[:10]
            )
            if raw_count > 10:
                summary += f"\n\n... and {raw_count - 10} more"

        user_msg = {
            "role": "user",
            "content": (
                f"{phase.message_template}\n\nCompacted context: {summary[:500]}"
            ),
        }

        result = TurnResult(
            turn_index=cfg.compaction_turn,
            phase_name=phase.name,
            phase_index=0,
            message_size_chars=len(json.dumps(user_msg)),
            tool_result_size_chars=len(summary),
        )

        return [result], summary, raw_count

    # ── Post-compact phase ────────────────────────────────────────────────

    def _run_post_compact(
        self,
        phase: PhaseConfig,
        cfg: SessionConfig,
        accumulated: list[str],
    ) -> tuple[list[TurnResult], list[str]]:
        results: list[TurnResult] = []
        n_findings = 0

        for turn_idx in range(phase.n_turns):
            task_idx = turn_idx + 1

            growth = (
                phase.tool_result_size_growth * turn_idx if phase.n_turns > 1 else 0
            )
            max_growth = (
                phase.tool_result_size_growth * (phase.n_turns - 1)
                if phase.n_turns > 1
                else 1
            )
            if max_growth > 0:
                tool_size = phase.tool_result_size_base + int(
                    (growth / max_growth)
                    * (cfg.max_tool_result_size - phase.tool_result_size_base)
                )
            else:
                tool_size = phase.tool_result_size_base

            n_findings += 1
            ctx = (
                phase.post_context_template
                if phase.post_context_template
                else (
                    f"Turn {turn_idx + 1}: continue analysis. "
                    f"{turn_idx + 1} tasks analyzed, {n_findings} findings."
                )
            )

            tmpl = phase.message_template
            header = tmpl.format(
                turn=turn_idx + 1,
                n_tasks=turn_idx + 1,
                n_findings=n_findings,
            )
            task_label2 = phase.task_template.format(idx=task_idx)
            content = f"{header}\n{ctx}\nTask: {task_label2}"
            user_msg = {"role": "user", "content": content}

            tool_text = self._format_tool_result(
                turn_idx + cfg.compaction_turn + 1, task_idx, tool_size
            )

            results.append(
                TurnResult(
                    turn_index=turn_idx + cfg.compaction_turn + 1,
                    phase_name=phase.name,
                    phase_index=turn_idx,
                    message_size_chars=len(json.dumps(user_msg)),
                    tool_result_size_chars=tool_size,
                )
            )
            accumulated.append(tool_text)

        return results, accumulated

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _format_tool_result(turn: int, task: int, size: int) -> str:
        """Format a tool_result as a structured text block."""
        prefix = f"Turn {turn}, Task {task} result: "
        padding = "x" * max(size - len(prefix), 1)
        return prefix + padding

    @staticmethod
    def _estimate_token_count(text: str) -> int:
        """Rough token count estimate (~4 chars per token for English)."""
        return max(len(text) // 4, 1)


# ---------------------------------------------------------------------------
# Session execution (driver-agnostic harness)
# ---------------------------------------------------------------------------


def _read_vllm_metrics(base_url: str) -> dict[str, float]:
    """Read vLLM /metrics counters.  Returns empty dict on failure."""
    try:
        req = urllib.request.Request(f"{base_url}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return {}
    counters: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\{[^}]*\}\s+([0-9eE+\-.]+)$", line)
        if not m:
            continue
        name = m.group(1)
        if name in (
            "vllm:prefix_cache_queries_total",
            "vllm:prefix_cache_hits_total",
            "vllm:prompt_tokens_cached_total",
        ):
            try:
                counters[name] = float(m.group(2))
            except ValueError:
                pass
    return counters


def _send_messages_litellm(
    base_url: str,
    messages: list[dict[str, str]],
    model: str,
    max_tokens: int = 500,
) -> dict[str, Any]:
    """Send Anthropic-format messages to a LiteLLM-compatible endpoint.

    Returns a dict with keys: ok, ttft_s, total_s, input_tokens,
    output_tokens, error.
    """
    flat = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        flat.append({"role": role, "content": content or "Continue."})

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": flat,
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": "dummy",
        },
    )
    t_start = time.monotonic()
    ttft_s: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if ttft_s is None and obj.get("type") == "content_block_delta":
                    ttft_s = time.monotonic() - t_start
                msg = obj.get("message") or obj
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    input_tokens = usage.get("input_tokens") or usage.get(
                        "prompt_tokens"
                    )
                    output_tokens = usage.get("output_tokens") or usage.get(
                        "completion_tokens"
                    )
    except urllib.error.HTTPError as exc:
        error = f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        error = str(exc)

    return {
        "ok": error is None,
        "ttft_s": ttft_s,
        "total_s": time.monotonic() - t_start,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_workload_session(
    workload: Workload,
    session_config: SessionConfig,
    *,
    base_url: str,
    model: str,
    max_tokens: int = 500,
    report: bool = True,
) -> WorkloadResult:
    """Execute a workload session against a backend.

    This is the workhorse function called by the runner.  It handles:

    1.  Generating the per-turn message sequence.
    2.  Sending each turn to the backend (via urllib + Anthropic protocol).
    3.  Capturing timing, token counts, and vLLM /metrics deltas.
    4.  Returning a ``WorkloadResult`` with per-phase breakdowns.
    """
    turns, compaction = workload.generate_turns(session_config)

    metrics_before = _read_vllm_metrics(base_url) if base_url else {}
    start = time.monotonic()

    for turn in turns:
        # Skip execution if no driver is available (dry-run / offline mode)
        if base_url == "":
            continue

        result = _send_messages_litellm(
            base_url,
            [{"role": "user", "content": f"Turn {turn.turn_index + 1}."}],
            model,
            max_tokens,
        )
        turn.ttft_s = result.get("ttft_s")
        turn.total_s = result.get("total_s")
        turn.input_tokens = result.get("input_tokens")
        turn.output_tokens = result.get("output_tokens")
        turn.error = result.get("error")

    wall_time = time.monotonic() - start
    metrics_after = _read_vllm_metrics(base_url) if base_url else {}

    phase_groups: dict[str, list[TurnResult]] = {}
    for t in turns:
        phase_groups.setdefault(t.phase_name, []).append(t)

    phases: list[PhaseResult] = []
    for name in ("pre-compact", "compact", "post-compact"):
        group = phase_groups.get(name, [])
        if not group:
            continue
        ttfts = [t.ttft_s for t in group if t.ttft_s is not None]
        times = [t.total_s for t in group if t.total_s is not None]
        in_tok = [t.input_tokens for t in group if t.input_tokens is not None]
        out_tok = [t.output_tokens for t in group if t.output_tokens is not None]
        tool_sz = [t.tool_result_size_chars for t in group]

        phases.append(
            PhaseResult(
                name=name,
                n_turns=len(group),
                prefill_avg_s=(sum(ttfts) / len(ttfts)) if ttfts else None,
                prefill_min_s=min(ttfts) if ttfts else None,
                prefill_max_s=max(ttfts) if ttfts else None,
                total_seconds=round(sum(times) / len(times), 3) if times else None,
                avg_input_tokens=(sum(in_tok) / len(in_tok) if in_tok else None),
                avg_output_tokens=(sum(out_tok) / len(out_tok) if out_tok else None),
                avg_tool_result_size=(sum(tool_sz) / len(tool_sz) if tool_sz else None),
            )
        )

    any_failure = any(t.error for t in turns)

    # Collect findings from pre-compact turns (first 5 unique by index)
    findings: list[str] = []
    seen_indices: set[int] = set()
    for t in turns:
        if t.phase_name == "pre-compact" and t.turn_index not in seen_indices:
            sample = f"Turn {t.turn_index} result (size={t.tool_result_size_chars})"
            findings.append(sample)
            seen_indices.add(t.turn_index)
            if len(findings) >= 5:
                break

    result = WorkloadResult(
        n_turns=session_config.n_turns,
        total_seconds=round(wall_time, 3),
        phases=phases,
        accumulated_findings=findings,
        compaction=compaction,
        any_failure=any_failure,
        avg_tool_result_size=round(
            sum(t.tool_result_size_chars for t in turns) / len(turns), 1
        )
        if turns
        else None,
        summary_message_size=sum(t.message_size_chars for t in turns),
    )

    if report:
        _print_report(result, metrics_before, metrics_after)

    return result


def _print_report(
    result: WorkloadResult,
    metrics_before: dict[str, float],
    metrics_after: dict[str, float],
) -> None:
    """Print a formatted session report to stdout."""
    print()
    print("=" * 60)
    print("WATCHER PATTERN WORKLOAD REPORT")
    print("=" * 60)

    # --- Summary ---
    print(f"\nTotal turns:      {result.n_turns}")
    print(f"Total time:       {result.total_seconds:.3f}s")
    print(f"Phase count:      {len(result.phases)}")
    if result.avg_tool_result_size is not None:
        print(f"Avg tool result:  {result.avg_tool_result_size:.0f} chars")
    if result.summary_message_size is not None:
        print(f"Total messages:   {result.summary_message_size} chars")
    if result.any_failure:
        print("STATUS: FAILURES DETECTED")

    # --- vLLM metrics delta ---
    print("\n--- vLLM /metrics delta ---")
    for key in (
        "vllm:prefix_cache_queries_total",
        "vllm:prefix_cache_hits_total",
        "vllm:prompt_tokens_cached_total",
    ):
        before = metrics_before.get(key)
        after = metrics_after.get(key)
        if before is not None and after is not None:
            delta = after - before
            print(f"  {key}: {before:>12.0f} -> {after:>12.0f} (delta: {delta:>+.0f})")
        else:
            print(f"  {key}: unavailable")

    # --- Phase breakdown ---
    print("\n--- Per-phase timing ---")
    print(
        f"{'Phase':<15} "
        f"{'Turns':>5} "
        f"{'TTFT avg':>10} "
        f"{'TTFT min':>10} "
        f"{'TTFT max':>10} "
        f"{'Time(s)':>10}"
    )
    print("-" * 60)
    for p in result.phases:
        ttft_avg = f"{p.prefill_avg_s:.3f}" if p.prefill_avg_s else "N/A"
        ttft_min = f"{p.prefill_min_s:.3f}" if p.prefill_min_s else "N/A"
        ttft_max = f"{p.prefill_max_s:.3f}" if p.prefill_max_s else "N/A"
        t = f"{p.total_seconds:.3f}" if p.total_seconds else "N/A"
        print(
            f"{p.name:<15} "
            f"{p.n_turns:>5} "
            f"{ttft_avg:>10} "
            f"{ttft_min:>10} "
            f"{ttft_max:>10} "
            f"{t:>10}"
        )

    # --- Compaction ---
    if result.compaction:
        print("\n--- Compaction ---")
        print(f"  Raw findings:   {result.compaction.get('raw_count', '?')}")
        print(f"  Summary size:   {result.compaction.get('summary_size', '?')} chars")

    # --- Token stats ---
    print("\n--- Token stats ---")
    for p in result.phases:
        in_tok = f"{p.avg_input_tokens:.0f}" if p.avg_input_tokens else "N/A"
        out_tok = f"{p.avg_output_tokens:.0f}" if p.avg_output_tokens else "N/A"
        print(f"  {p.name}: avg in={in_tok}  out={out_tok}")

    print()
    print("=" * 60)


def run_bench_workload_session(
    workload: Workload,
    session_config: SessionConfig,
    *,
    base_url: str,
    model: str,
    max_tokens: int = 500,
    report: bool = True,
) -> WorkloadResult:
    """Alias of :func:`run_workload_session` for the existing runner entry point."""
    return run_workload_session(
        workload,
        session_config,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        report=report,
    )
