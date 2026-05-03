"""WOR-344 — probe vLLM's native Anthropic Messages API.

Run this against a vLLM 0.20.0+ server already serving on localhost:8000.

Tests, in order:
    1. /v1/models           — sanity check the served model name
    2. /v1/messages         — non-streaming text response (Anthropic shape)
    3. /v1/messages stream  — SSE event roundtrip
    4. /v1/messages tool    — tool_use block returned for a defined tool
    5. tool_result follow-up — pass tool_result back, expect natural language
    6. /v1/messages/count_tokens — token-count endpoint

Each test prints PASS / FAIL / SKIP plus a one-line reason. The script exits
non-zero if any required test fails. Stdlib only — no anthropic SDK needed.

Usage:
    python scripts/spikes/wor344_vllm_anthropic_probe.py
    python scripts/spikes/wor344_vllm_anthropic_probe.py --base-url http://localhost:8000 --model qwen3-coder
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _post(
    url: str, payload: dict[str, Any], timeout: float = 60.0
) -> tuple[int, bytes]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": "dummy",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_stream(
    url: str, payload: dict[str, Any], timeout: float = 60.0
) -> tuple[int, list[str]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": "2023-06-01",
            "x-api-key": "dummy",
        },
    )
    events: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    events.append(line)
            return resp.status, events
    except urllib.error.HTTPError as e:
        return e.code, [e.read().decode("utf-8", errors="replace")]


def _get(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status: str = "PENDING"
        self.detail: str = ""
        self.elapsed_ms: int = 0

    def passed(self, detail: str = "") -> None:
        self.status = "PASS"
        self.detail = detail

    def failed(self, detail: str) -> None:
        self.status = "FAIL"
        self.detail = detail

    def skipped(self, detail: str) -> None:
        self.status = "SKIP"
        self.detail = detail

    def __str__(self) -> str:
        marker = {"PASS": "[OK]  ", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}.get(
            self.status, "[??]  "
        )
        line = f"{marker} {self.name} ({self.elapsed_ms} ms)"
        if self.detail:
            line += f"\n       {self.detail}"
        return line


def _time(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, int]:
    t0 = time.perf_counter()
    out = fn(*args, **kwargs)
    return out, int((time.perf_counter() - t0) * 1000)


def test_models_endpoint(
    base_url: str, model_hint: str | None
) -> tuple[Result, str | None]:
    r = Result("/v1/models — sanity")
    (code, body), r.elapsed_ms = _time(_get, f"{base_url}/v1/models")
    if code != 200:
        r.failed(f"HTTP {code}: {body[:200]!r}")
        return r, None
    try:
        data = json.loads(body)
        ids = [m["id"] for m in data.get("data", [])]
    except Exception as e:
        r.failed(f"could not parse response: {e}")
        return r, None
    if not ids:
        r.failed("no models served")
        return r, None
    chosen = model_hint if model_hint and model_hint in ids else ids[0]
    r.passed(f"served: {ids}  using: {chosen!r}")
    return r, chosen


def test_messages_basic(base_url: str, model: str) -> Result:
    r = Result("/v1/messages — non-streaming")
    payload = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
    }
    (code, body), r.elapsed_ms = _time(_post, f"{base_url}/v1/messages", payload)
    if code != 200:
        r.failed(f"HTTP {code}: {body[:300]!r}")
        return r
    try:
        data = json.loads(body)
    except Exception as e:
        r.failed(f"could not parse JSON: {e}; body={body[:200]!r}")
        return r
    if data.get("type") != "message":
        r.failed(f"expected type=message, got {data.get('type')!r}; full={data!r}")
        return r
    blocks = data.get("content", [])
    block_types = [b.get("type") for b in blocks]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    thinking = "".join(
        b.get("thinking", "") for b in blocks if b.get("type") == "thinking"
    )
    if not text and not thinking:
        r.failed(
            f"no text or thinking block in response: blocks={block_types} full={data!r}"
        )
        return r
    snippet = text[:80] if text else f"thinking_only={thinking[:80]!r}"
    r.passed(
        f"stop_reason={data.get('stop_reason')} usage={data.get('usage')} blocks={block_types} text={snippet!r}"
    )
    return r


def test_messages_stream(base_url: str, model: str) -> Result:
    r = Result("/v1/messages — streaming SSE")
    payload = {
        "model": model,
        "max_tokens": 256,
        "stream": True,
        "messages": [{"role": "user", "content": "Count to three."}],
    }
    (code, events), r.elapsed_ms = _time(
        _post_stream, f"{base_url}/v1/messages", payload
    )
    if code != 200:
        r.failed(f"HTTP {code}: {events[:1]!r}")
        return r
    event_types: list[str] = []
    for line in events:
        if line.startswith("event: "):
            event_types.append(line.removeprefix("event: ").strip())
    required = {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "message_stop",
    }
    missing = required - set(event_types)
    if missing:
        r.failed(
            f"missing required SSE events: {sorted(missing)}; got {event_types[:10]}"
        )
        return r
    r.passed(f"events seen: {sorted(set(event_types))}")
    return r


_TOOL_DEF = {
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


def test_messages_tool_use(
    base_url: str, model: str
) -> tuple[Result, dict[str, Any] | None]:
    r = Result("/v1/messages — tool_use roundtrip")
    payload = {
        "model": model,
        "max_tokens": 256,
        "tools": [_TOOL_DEF],
        "messages": [
            {
                "role": "user",
                "content": "What is the weather in Helsinki right now? Use the tool.",
            }
        ],
    }
    (code, body), r.elapsed_ms = _time(_post, f"{base_url}/v1/messages", payload)
    if code != 200:
        r.failed(f"HTTP {code}: {body[:300]!r}")
        return r, None
    try:
        data = json.loads(body)
    except Exception as e:
        r.failed(f"could not parse JSON: {e}")
        return r, None
    blocks = data.get("content", [])
    tool_use = next((b for b in blocks if b.get("type") == "tool_use"), None)
    if tool_use is None:
        r.failed(
            f"no tool_use block in response. stop_reason={data.get('stop_reason')} blocks={[b.get('type') for b in blocks]}"
        )
        return r, None
    if tool_use.get("name") != "get_weather":
        r.failed(f"wrong tool name: {tool_use.get('name')!r}")
        return r, None
    if data.get("stop_reason") != "tool_use":
        r.failed(f"expected stop_reason=tool_use, got {data.get('stop_reason')!r}")
        return r, None
    r.passed(f"id={tool_use.get('id')} input={tool_use.get('input')!r}")
    return r, data


def test_tool_result_followup(
    base_url: str, model: str, prior_assistant_msg: dict[str, Any]
) -> Result:
    r = Result("/v1/messages — tool_result followup")
    tool_use = next(
        (
            b
            for b in prior_assistant_msg.get("content", [])
            if b.get("type") == "tool_use"
        ),
        None,
    )
    if tool_use is None:
        r.skipped("no prior tool_use to continue from")
        return r
    payload = {
        "model": model,
        "max_tokens": 256,
        "tools": [_TOOL_DEF],
        "messages": [
            {
                "role": "user",
                "content": "What is the weather in Helsinki right now? Use the tool.",
            },
            {"role": "assistant", "content": prior_assistant_msg["content"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use["id"],
                        "content": "Helsinki: 4 C, partly cloudy.",
                    }
                ],
            },
        ],
    }
    (code, body), r.elapsed_ms = _time(_post, f"{base_url}/v1/messages", payload)
    if code != 200:
        r.failed(f"HTTP {code}: {body[:300]!r}")
        return r
    try:
        data = json.loads(body)
    except Exception as e:
        r.failed(f"could not parse JSON: {e}")
        return r
    blocks = data.get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text:
        r.failed(f"no text in followup; blocks={[b.get('type') for b in blocks]}")
        return r
    if data.get("stop_reason") not in {"end_turn", "stop_sequence", "max_tokens"}:
        r.failed(f"unexpected stop_reason={data.get('stop_reason')!r}")
        return r
    r.passed(f"stop_reason={data.get('stop_reason')} text={text[:120]!r}")
    return r


def test_count_tokens(base_url: str, model: str) -> Result:
    r = Result("/v1/messages/count_tokens")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hello world."}],
    }
    (code, body), r.elapsed_ms = _time(
        _post, f"{base_url}/v1/messages/count_tokens", payload
    )
    if code == 404:
        r.skipped("endpoint not implemented in this build")
        return r
    if code != 200:
        r.failed(f"HTTP {code}: {body[:300]!r}")
        return r
    try:
        data = json.loads(body)
    except Exception as e:
        r.failed(f"could not parse JSON: {e}")
        return r
    if "input_tokens" not in data:
        r.failed(f"missing input_tokens key: {data!r}")
        return r
    r.passed(f"input_tokens={data['input_tokens']}")
    return r


def main() -> int:
    p = argparse.ArgumentParser(description="WOR-344 vLLM native Anthropic API probe")
    p.add_argument("--base-url", default="http://localhost:8000", help="vLLM base URL")
    p.add_argument(
        "--model",
        default=None,
        help="served model name (auto-detected from /v1/models if omitted)",
    )
    args = p.parse_args()

    print("--- WOR-344 vLLM Anthropic Messages API probe ---")
    print(f"base_url: {args.base_url}")

    results: list[Result] = []

    r0, model = test_models_endpoint(args.base_url, args.model)
    results.append(r0)
    print(r0)
    if model is None:
        print("\nFATAL: cannot continue without a model name.")
        return 1

    for fn in (test_messages_basic, test_messages_stream):
        r = fn(args.base_url, model)
        results.append(r)
        print(r)

    r3, asst_msg = test_messages_tool_use(args.base_url, model)
    results.append(r3)
    print(r3)

    r4 = (
        test_tool_result_followup(args.base_url, model, asst_msg)
        if asst_msg
        else Result("/v1/messages — tool_result followup")
    )
    if asst_msg is None:
        r4.skipped("upstream tool_use test failed")
    results.append(r4)
    print(r4)

    r5 = test_count_tokens(args.base_url, model)
    results.append(r5)
    print(r5)

    failed = [r for r in results if r.status == "FAIL"]
    skipped = [r for r in results if r.status == "SKIP"]
    print()
    print(
        f"Summary: {len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped"
    )
    if failed:
        print("Failed:")
        for r in failed:
            print(f"  - {r.name}: {r.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
