"""WOR-287 — controlled prefix-cache test.

Sends N requests with an identical large prefix and a variant short
suffix to each backend (vLLM-direct and via LiteLLM). If the prefix
cache is firing, T2..TN should have dramatically lower TTFT than T1.

Reads vLLM's `/metrics` before and after each request to attribute
cache hits to specific requests.

Usage:
    python scripts/spikes/wor287_prefix_cache_test.py
    python scripts/spikes/wor287_prefix_cache_test.py --requests 5

Exit codes:
    0  ok
    2  vLLM or LiteLLM unreachable
    3  experiment ran but at least one request failed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VLLM_URL = "http://localhost:8000"
_LITELLM_URL = "http://localhost:8082"
_VLLM_MODEL = "/home/antti/models/Qwen3.6-35B-A3B-NVFP4"
_LITELLM_MODEL = "claude-sonnet-4-6"
_ARTIFACT_DIR = Path("docs/spikes/_wor287_artifacts")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PREFIX_FILES = [
    "app/core/watcher/watcher.py",
    "app/core/watcher/watcher_finalize.py",
    "app/core/watcher/watcher_subprocess.py",
]

_VARIANT_SUFFIXES = [
    "Briefly: what is the entry point?",
    "Briefly: which classes are exported?",
    "Briefly: what is the watcher poll interval?",
    "Briefly: how many tests are in the suite?",
    "Briefly: what is the file size of watcher.py?",
]


def _build_prefix() -> str:
    parts: list[str] = []
    for rel in _PREFIX_FILES:
        path = _REPO_ROOT / rel
        parts.append(f"# === {rel} ===\n{path.read_text(encoding='utf-8')}\n")
    body = "\n".join(parts)
    return (
        "You are reading source code from a Python project. After the code, "
        "I will ask you a brief question about it. Answer in one sentence.\n\n"
        f"{body}\n\n---\n\n"
    )


def _read_vllm_counters() -> dict[str, float]:
    """Snapshot just the counters we care about."""
    try:
        req = urllib.request.Request(f"{_VLLM_URL}/metrics")
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return {}
    keys = (
        "vllm:prefix_cache_queries_total",
        "vllm:prefix_cache_hits_total",
        "vllm:prompt_tokens_cached_total",
    )
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)\{[^}]*\}\s+([0-9eE+\-.]+)$", line)
        if not m:
            continue
        name = m.group(1)
        if name in keys:
            try:
                # If multiple lines (multiple labels), keep the last (single instance expected)
                out[name] = float(m.group(2))
            except ValueError:
                pass
    return out


def _send_openai(
    base_url: str, prefix: str, suffix: str, model: str, max_tokens: int
) -> dict[str, Any]:
    """Send to vLLM-direct (OpenAI /v1/chat/completions, streaming)."""
    body = {
        "model": model,
        "messages": [
            {"role": "user", "content": prefix + suffix},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    return _stream_and_time(req, format_="openai")


def _send_anthropic(
    base_url: str, prefix: str, suffix: str, model: str, max_tokens: int
) -> dict[str, Any]:
    """Send to LiteLLM (Anthropic /v1/messages, streaming)."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prefix + suffix}],
            }
        ],
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
    return _stream_and_time(req, format_="anthropic")


def _stream_and_time(req: urllib.request.Request, format_: str) -> dict[str, Any]:
    t_start = time.monotonic()
    ttft_s: float | None = None
    decode_first_seen = False
    bytes_seen = 0
    usage: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310
            for raw in resp:
                bytes_seen += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                # Both Anthropic and OpenAI use SSE; lines are "data: ..." or "event: ..."
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # Detect first content-bearing event for TTFT
                if not decode_first_seen and _is_content_delta(obj, format_):
                    ttft_s = time.monotonic() - t_start
                    decode_first_seen = True
                u = _extract_usage(obj, format_)
                if u:
                    usage = {**usage, **u}
        total_s = time.monotonic() - t_start
        return {
            "ok": True,
            "ttft_s": ttft_s,
            "total_s": total_s,
            "bytes": bytes_seen,
            "usage": usage,
        }
    except urllib.error.HTTPError as exc:
        body_excerpt = exc.read()[:300].decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "error": body_excerpt,
            "total_s": time.monotonic() - t_start,
        }
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": str(exc), "total_s": time.monotonic() - t_start}


def _is_content_delta(obj: dict[str, Any], format_: str) -> bool:
    if format_ == "openai":
        choices = obj.get("choices") or []
        for ch in choices:
            delta = ch.get("delta") or {}
            if isinstance(delta, dict) and any(
                # vLLM/Qwen3 emits "reasoning" (thinking) before "content"
                delta.get(k)
                for k in ("content", "reasoning_content", "reasoning")
            ):
                return True
        return False
    # anthropic
    if obj.get("type") == "content_block_delta":
        return True
    return False


def _extract_usage(obj: dict[str, Any], format_: str) -> dict[str, Any]:
    if format_ == "openai":
        u = obj.get("usage")
        return u if isinstance(u, dict) else {}
    # anthropic message_delta has usage on type==message_start or message_delta
    if obj.get("type") in {"message_start", "message_delta"}:
        msg = obj.get("message") or obj
        u = msg.get("usage")
        return u if isinstance(u, dict) else {}
    return {}


def _run_backend(
    name: str,
    backend_url: str,
    sender: Any,
    model: str,
    prefix: str,
    n_requests: int,
    max_tokens: int,
) -> dict[str, Any]:
    print(f"\n=== Backend: {name} ({backend_url}) ===")
    backend_start_counters = _read_vllm_counters()
    print(
        f"  vLLM counters before: queries={backend_start_counters.get('vllm:prefix_cache_queries_total', 0):,.0f}  "
        f"hits={backend_start_counters.get('vllm:prefix_cache_hits_total', 0):,.0f}  "
        f"tokens_cached={backend_start_counters.get('vllm:prompt_tokens_cached_total', 0):,.0f}"
    )

    requests_log: list[dict[str, Any]] = []
    for i, suffix in enumerate(_VARIANT_SUFFIXES[:n_requests], 1):
        before = _read_vllm_counters()
        r = sender(backend_url, prefix, suffix, model, max_tokens)
        after = _read_vllm_counters()
        delta_queries = after.get("vllm:prefix_cache_queries_total", 0) - before.get(
            "vllm:prefix_cache_queries_total", 0
        )
        delta_hits = after.get("vllm:prefix_cache_hits_total", 0) - before.get(
            "vllm:prefix_cache_hits_total", 0
        )
        delta_tokens = after.get("vllm:prompt_tokens_cached_total", 0) - before.get(
            "vllm:prompt_tokens_cached_total", 0
        )
        ttft_str = f"{r.get('ttft_s'):.2f}s" if r.get("ttft_s") is not None else "-"
        total_str = f"{r.get('total_s'):.2f}s" if r.get("total_s") is not None else "-"
        ok_marker = "OK" if r.get("ok") else "FAIL"
        print(
            f"  [T{i}] {ok_marker:4} ttft={ttft_str:>8}  total={total_str:>8}  "
            f"d_queries={delta_queries:+,.0f}  d_hits={delta_hits:+,.0f}  d_cached_tok={delta_tokens:+,.0f}"
        )
        if not r.get("ok"):
            print(f"       error: {r.get('error', '?')}")
        requests_log.append(
            {
                "turn": i,
                "suffix": suffix,
                "result": r,
                "delta_queries": delta_queries,
                "delta_hits": delta_hits,
                "delta_tokens_cached": delta_tokens,
            }
        )

    # Heuristic verdict
    ttfts = [
        rl["result"].get("ttft_s")
        for rl in requests_log
        if rl["result"].get("ok") and rl["result"].get("ttft_s")
    ]
    verdict = "INSUFFICIENT DATA"
    if len(ttfts) >= 3:
        t1 = ttfts[0]
        rest = ttfts[1:]
        rest_mean = sum(rest) / len(rest)
        ratio = rest_mean / t1 if t1 > 0 else 1.0
        if ratio < 0.5:
            verdict = f"CACHE FIRING (T2..TN mean {rest_mean:.2f}s vs T1 {t1:.2f}s — {(1 - ratio) * 100:.0f}% reduction)"
        elif ratio < 0.85:
            verdict = f"CACHE PARTIALLY FIRING (T2..TN mean {rest_mean:.2f}s vs T1 {t1:.2f}s — {(1 - ratio) * 100:.0f}% reduction)"
        else:
            verdict = f"CACHE NOT FIRING (T2..TN mean {rest_mean:.2f}s vs T1 {t1:.2f}s — only {(1 - ratio) * 100:.0f}% reduction)"
    print(f"\n  Verdict: {verdict}")
    return {
        "backend": name,
        "url": backend_url,
        "counters_before": backend_start_counters,
        "counters_after": _read_vllm_counters(),
        "requests": requests_log,
        "verdict": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=80)
    args = parser.parse_args()

    prefix = _build_prefix()
    print(
        f"Prefix size: {len(prefix):,} chars (~{len(prefix) // 4:,} tokens estimated)"
    )
    print(f"Variant suffixes: {len(_VARIANT_SUFFIXES)}")
    print(f"Requests per backend: {args.requests}")
    print(f"Max output tokens: {args.max_tokens}")

    # Warm-up — one throwaway through vLLM to flush JIT compilation noise
    print("\nWarming up vLLM with one throwaway request …")
    _send_openai(_VLLM_URL, "Hello", " world", _VLLM_MODEL, 5)

    results: list[dict[str, Any]] = []
    results.append(
        _run_backend(
            "vLLM-direct (OpenAI)",
            _VLLM_URL,
            _send_openai,
            _VLLM_MODEL,
            prefix,
            args.requests,
            args.max_tokens,
        )
    )
    results.append(
        _run_backend(
            "LiteLLM (Anthropic)",
            _LITELLM_URL,
            _send_anthropic,
            _LITELLM_MODEL,
            prefix,
            args.requests,
            args.max_tokens,
        )
    )

    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _ARTIFACT_DIR / f"prefix_test_{ts}.json"
    out_path.write_text(
        json.dumps(
            {
                "timestamp_utc": ts,
                "prefix_chars": len(prefix),
                "n_requests": args.requests,
                "max_tokens": args.max_tokens,
                "backends": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nArtifact: {out_path}")

    any_fail = any(not rl["result"].get("ok") for r in results for rl in r["requests"])
    return 3 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
