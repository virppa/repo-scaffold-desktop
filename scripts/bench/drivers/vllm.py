"""vLLM backend driver: SSE streaming via urllib POST /v1/chat/completions."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from scripts.bench.drivers.base import GenerationResult

logger = logging.getLogger(__name__)

_SSE_PREFIX = "data: "
_SSE_DONE = "[DONE]"
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _validated_base_url(url: str) -> str:
    scheme = urlparse(url).scheme
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"base_url must use http or https scheme, got: {scheme!r}")
    return url.rstrip("/")


class VllmDriver:
    """Implements BackendDriver for vLLM's /v1/chat/completions SSE endpoint."""

    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self._base_url = _validated_base_url(base_url)

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self._base_url}/health")
            with urllib.request.urlopen(req, timeout=2):
                return True
        except Exception:
            return False

    def generate(self, model: str, messages: list[dict[str, str]]) -> GenerationResult:
        payload = json.dumps(
            {
                "model": model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        ).encode()
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            t_start = time.monotonic()
            with urllib.request.urlopen(req, timeout=300) as resp:
                return self._parse_streaming(resp, t_start)
        except urllib.error.URLError as exc:
            return GenerationResult(error=str(exc))
        except Exception as exc:
            return GenerationResult(error=str(exc))

    def _parse_streaming(self, resp: Any, t_start: float) -> GenerationResult:
        ttft_s: float | None = None
        text_parts: list[str] = []
        input_tokens: int | None = None
        output_tokens: int | None = None

        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line.startswith(_SSE_PREFIX):
                continue
            data = line[len(_SSE_PREFIX) :]
            if data == _SSE_DONE:
                break
            try:
                frame: dict[str, Any] = json.loads(data)
            except json.JSONDecodeError:
                continue

            usage = frame.get("usage")
            if usage:
                input_tokens = usage.get("prompt_tokens")
                output_tokens = usage.get("completion_tokens")

            for choice in frame.get("choices", []):
                content: str = choice.get("delta", {}).get("content") or ""
                if content:
                    if ttft_s is None:
                        ttft_s = time.monotonic() - t_start
                    text_parts.append(content)

        return GenerationResult(
            text="".join(text_parts),
            ttft_s=ttft_s,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
