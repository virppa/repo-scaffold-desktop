"""Ollama backend driver: streaming NDJSON via urllib POST /api/chat."""

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

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _validated_base_url(url: str) -> str:
    scheme = urlparse(url).scheme
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"base_url must use http or https scheme, got: {scheme!r}")
    return url.rstrip("/")


class OllamaDriver:
    """Implements BackendDriver for Ollama's /api/chat streaming endpoint."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self._base_url = _validated_base_url(base_url)

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self._base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=2):
                return True
        except Exception:
            return False

    def generate(self, model: str, messages: list[dict[str, str]]) -> GenerationResult:
        payload = json.dumps(
            {"model": model, "messages": messages, "stream": True}
        ).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/chat",
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
        final_frame: dict[str, Any] = {}

        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            try:
                frame: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not frame.get("done", False):
                content: str = frame.get("message", {}).get("content", "")
                if content and ttft_s is None:
                    ttft_s = time.monotonic() - t_start
                text_parts.append(content)
            else:
                final_frame = frame

        load_ns: int = final_frame.get("load_duration") or 0
        raw_load_duration_ns: int | None = load_ns if load_ns > 0 else None

        raw_eval_duration_ns: int | None = final_frame.get("eval_duration")
        decode_time_s: float | None = (
            raw_eval_duration_ns / 1e9 if raw_eval_duration_ns is not None else None
        )

        return GenerationResult(
            text="".join(text_parts),
            ttft_s=ttft_s,
            decode_time_s=decode_time_s,
            raw_prompt_eval_duration_ns=final_frame.get("prompt_eval_duration"),
            raw_eval_duration_ns=raw_eval_duration_ns,
            raw_load_duration_ns=raw_load_duration_ns,
            input_tokens=final_frame.get("prompt_eval_count"),
            output_tokens=final_frame.get("eval_count"),
        )
