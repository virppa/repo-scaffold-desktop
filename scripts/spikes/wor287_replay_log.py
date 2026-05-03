"""WOR-287 — replay WOR-322 turn sequence and measure prefill behavior.

Reads `.claude/artifacts/wor_322/worker_wor-322.log`, reconstructs the
exact Anthropic-format messages Claude Code sent at each of the 17 turns,
and replays them sequentially through LiteLLM (the production path).

For each replayed turn we capture:
- TTFT (proxy for prefill latency — dominates wall time at low max_tokens)
- vLLM Prefix-cache hit-rate (parsed from the metrics endpoint, not
  the misleading raw counters)
- Total wall time

The interesting signal: does each subsequent turn's TTFT stay constant
(cache helping) or grow with input size (cache not helping)?

Usage:
    python scripts/spikes/wor287_replay_log.py
    python scripts/spikes/wor287_replay_log.py --max-tokens 5 --limit 5

Exit codes:
    0  ok
    2  log file missing or unreadable
    3  one or more replay turns failed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VLLM_URL = "http://localhost:8000"
_LITELLM_URL = "http://localhost:8082"
_LITELLM_MODEL = "claude-sonnet-4-6"
_ARTIFACT_DIR = Path("docs/spikes/_wor287_artifacts")
_LOG_PATH = Path(".claude/artifacts/wor_322/worker_wor-322.log")


def _flatten_block(block: dict[str, Any]) -> str:
    """Flatten one Anthropic-format content block to plain text.

    vLLM's chat template can't ingest tool_use / tool_result blocks
    directly — flattening preserves token volume (the cache-cliff
    signal we care about) while making the request acceptable.
    """
    bt = block.get("type")
    if bt == "text":
        text: str = block.get("text", "")
        return text
    if bt == "thinking":
        return f"<thinking>{block.get('thinking', '')}</thinking>"
    if bt == "tool_use":
        name = block.get("name", "")
        inp = json.dumps(block.get("input", {}), default=str)
        return f"<tool_use name={name!r}>{inp}</tool_use>"
    if bt == "tool_result":
        content = block.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                c.get("text", str(c)) if isinstance(c, dict) else str(c)
                for c in content
            )
        return f"<tool_result>{content}</tool_result>"
    return json.dumps(block, default=str)


def _flatten_message(msg: dict[str, Any]) -> dict[str, str]:
    role = msg.get("role", "user")
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            _flatten_block(b) if isinstance(b, dict) else str(b) for b in content
        )
    else:
        text = str(content)
    return {"role": role, "content": text}


def _load_messages_per_turn(log_path: Path) -> list[list[dict[str, Any]]]:
    """Reconstruct the messages-list snapshot at each LLM turn.

    Returns: list where index i is the messages-list Claude Code sent
    to produce assistant turn (i+1). Length = number of unique assistant
    turns in the log.
    """
    events = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    # First user message event gives us the seed (initial prompt + manifest read)
    # We walk events in order, grouping assistant events by message_id and
    # user events by their position in the stream.

    # Group assistant events by message id (multiple events per message — one
    # per content block type)
    grouped_assistants: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"content": [], "first_idx": None}
    )
    asst_order: list[str] = []
    for i, ev in enumerate(events):
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message", {}) or {}
        mid = msg.get("id", f"_anon_asst_{i}")
        g = grouped_assistants[mid]
        if g["first_idx"] is None:
            g["first_idx"] = i
            asst_order.append(mid)
        for block in msg.get("content", []):
            if isinstance(block, dict):
                g["content"].append(block)

    # Walk events, building the messages-list snapshot just BEFORE each
    # assistant turn fires
    messages_so_far: list[dict[str, Any]] = []
    snapshots: list[list[dict[str, Any]]] = []
    consumed_asst_ids: set[str] = set()
    for ev in events:
        et = ev.get("type")
        if et == "user":
            msg = ev.get("message", {}) or {}
            content = msg.get("content")
            if content is None:
                continue
            messages_so_far.append({"role": "user", "content": content})
        elif et == "assistant":
            msg = ev.get("message", {}) or {}
            mid = msg.get("id")
            if mid is None or mid in consumed_asst_ids:
                continue
            # snapshot the messages-list state BEFORE this assistant message
            # — this is what Claude Code sent to provoke this assistant response
            snapshots.append([dict(m) for m in messages_so_far])
            consumed_asst_ids.add(mid)
            # Now add this assistant's content to the messages list
            full_content = grouped_assistants[mid]["content"]
            messages_so_far.append({"role": "assistant", "content": full_content})

    return snapshots


def _read_vllm_hit_rate() -> tuple[float | None, dict[str, float]]:
    """Read vLLM's authoritative interval-aggregated hit rate.

    vLLM exposes this via the /metrics endpoint as scattered counters,
    but it ALSO logs a `Prefix cache hit rate: X%` line periodically.
    Since we can't read the log from here, we compute the running
    delta-based proxy: hits / queries over a small sample window.
    """
    try:
        req = urllib.request.Request(f"{_VLLM_URL}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return None, {}
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
    q = counters.get("vllm:prefix_cache_queries_total", 0)
    h = counters.get("vllm:prefix_cache_hits_total", 0)
    rate = (h / q) if q > 0 else None
    return rate, counters


def _replay_turn_litellm(
    messages: list[dict[str, Any]], max_tokens: int
) -> dict[str, Any]:
    """Send to LiteLLM Anthropic /v1/messages, stream, capture TTFT.

    Anthropic-format messages with pure tool_result content lists are
    rejected by vLLM's Qwen3-coder chat template (`No user query found
    in messages`). We flatten every block to plain text — this preserves
    the input volume (cache-cliff signal) but no longer reproduces the
    exact original token sequence.
    """
    flat = [_flatten_message(m) for m in messages]
    # Ensure first message is a real user query — chat template requires it
    if not flat or flat[0]["role"] != "user" or not flat[0]["content"].strip():
        flat = [
            {
                "role": "user",
                "content": "Continue implementing WOR-322 per the manifest.",
            }
        ] + flat
    body = {
        "model": _LITELLM_MODEL,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": flat,
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{_LITELLM_URL}/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": "dummy",
        },
    )
    t_start = time.monotonic()
    ttft_s: float | None = None
    bytes_seen = 0
    usage: dict[str, Any] = {}
    error: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:  # nosec B310
            for raw in resp:
                bytes_seen += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if ttft_s is None and obj.get("type") == "content_block_delta":
                    ttft_s = time.monotonic() - t_start
                if obj.get("type") in {"message_start", "message_delta"}:
                    msg = obj.get("message") or obj
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        usage = {**usage, **u}
    except urllib.error.HTTPError as exc:
        body_excerpt = exc.read()[:300].decode("utf-8", errors="replace")
        error = f"HTTP {exc.code}: {body_excerpt}"
    except (urllib.error.URLError, OSError) as exc:
        error = str(exc)
    total_s = time.monotonic() - t_start
    return {
        "ok": error is None,
        "ttft_s": ttft_s,
        "total_s": total_s,
        "bytes": bytes_seen,
        "usage": usage,
        "error": error,
    }


def _estimate_input_chars(messages: list[dict[str, Any]]) -> int:
    """Rough size estimate for log/display."""
    return len(json.dumps(messages, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Replay only the first N turns (default: all 17)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=10,
        help="Cap output tokens per turn — we want prefill latency, not full responses",
    )
    args = parser.parse_args()

    if not _LOG_PATH.exists():
        print(f"ERROR: {_LOG_PATH} not found", file=sys.stderr)
        return 2

    print(f"Loading {_LOG_PATH} …")
    snapshots = _load_messages_per_turn(_LOG_PATH)
    print(f"Reconstructed {len(snapshots)} turn snapshots")

    n = min(args.limit or len(snapshots), len(snapshots))
    print(f"Replaying first {n} turns through LiteLLM ({_LITELLM_URL})")
    print(f"max_tokens per turn: {args.max_tokens} (we want prefill, not generation)")

    rate_pre, ctr_pre = _read_vllm_hit_rate()
    print(
        f"\nvLLM lifetime hit rate before replay: {rate_pre:.3%}"
        if rate_pre is not None
        else "\nvLLM hit rate unavailable"
    )

    print(
        f"\n{'turn':>4}  {'msgs':>5}  {'in_chars':>10}  {'ttft_s':>8}  {'total_s':>8}  {'usage':>20}"
    )
    print(f"{'-' * 4}  {'-' * 5}  {'-' * 10}  {'-' * 8}  {'-' * 8}  {'-' * 20}")
    turn_results: list[dict[str, Any]] = []
    for i in range(n):
        messages = snapshots[i]
        if not messages:
            print(f"{i + 1:>4}  (empty snapshot — skipping)")
            continue
        in_chars = _estimate_input_chars(messages)
        r = _replay_turn_litellm(messages, args.max_tokens)
        ttft_str = f"{r['ttft_s']:.2f}" if r.get("ttft_s") is not None else "-"
        total_str = f"{r['total_s']:.2f}"
        u = r.get("usage", {})
        usage_str = f"in={u.get('input_tokens', '?')} out={u.get('output_tokens', '?')}"
        ok_marker = "OK" if r["ok"] else "FAIL"
        print(
            f"{i + 1:>4}  {len(messages):>5}  {in_chars:>10,}  {ttft_str:>8}  {total_str:>8}  {usage_str:>20}  {ok_marker}"
        )
        if not r["ok"]:
            print(f"      error: {r.get('error', '?')}")
        turn_results.append(
            {
                "turn": i + 1,
                "n_messages": len(messages),
                "input_chars": in_chars,
                "result": r,
            }
        )

    rate_post, ctr_post = _read_vllm_hit_rate()
    if rate_post is not None and rate_pre is not None:
        delta_q = ctr_post.get("vllm:prefix_cache_queries_total", 0) - ctr_pre.get(
            "vllm:prefix_cache_queries_total", 0
        )
        delta_h = ctr_post.get("vllm:prefix_cache_hits_total", 0) - ctr_pre.get(
            "vllm:prefix_cache_hits_total", 0
        )
        delta_t = ctr_post.get("vllm:prompt_tokens_cached_total", 0) - ctr_pre.get(
            "vllm:prompt_tokens_cached_total", 0
        )
        print(
            f"\nvLLM lifetime hit rate after replay: {rate_post:.3%} (was {rate_pre:.3%})"
        )
        print(
            f"During-replay deltas: queries=+{delta_q:,.0f} hits=+{delta_h:,.0f} cached_tokens=+{delta_t:,.0f}"
        )

    # Save artifact
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _ARTIFACT_DIR / f"replay_{ts}.json"
    out_path.write_text(
        json.dumps(
            {
                "timestamp_utc": ts,
                "log_source": str(_LOG_PATH),
                "n_turns_replayed": len(turn_results),
                "max_tokens": args.max_tokens,
                "vllm_hit_rate_before": rate_pre,
                "vllm_hit_rate_after": rate_post,
                "counters_before": ctr_pre,
                "counters_after": ctr_post,
                "turns": turn_results,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nArtifact: {out_path}")

    any_fail = any(not t["result"].get("ok") for t in turn_results)
    return 3 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
