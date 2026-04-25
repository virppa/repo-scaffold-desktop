"""Ollama lifecycle manager: ensure_running, pull_if_needed, flush_model, get_ps_status.

Uses stdlib urllib only — no third-party HTTP dependencies.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 11434
_ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass
class ModelStatus:
    """Status of a loaded model from /api/ps."""

    name: str
    size: int
    size_vram: int
    processor_status: str
    expires_at: str


class OllamaManager:
    """Manages Ollama process lifecycle and model state."""

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"base_url must use http or https scheme, got: {parsed.scheme!r}"
            )
        self._base_url = base_url.rstrip("/")
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or _DEFAULT_PORT

    def ensure_running(self, timeout: float = 120.0) -> None:
        """Wait until Ollama is reachable via TCP probe then HTTP /api/tags."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                if sock.connect_ex((self._host, self._port)) != 0:
                    time.sleep(0.5)
                    continue
            try:
                conn = http.client.HTTPConnection(self._host, self._port, timeout=2)
                conn.request("GET", "/api/tags")
                if conn.getresponse().status == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.5)
        raise TimeoutError(f"Ollama not ready after {timeout}s")

    def pull_if_needed(self, model: str) -> None:
        """Pull model only if it is not already listed in /api/tags."""
        try:
            req = urllib.request.Request(f"{self._base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            existing = {m["name"] for m in data.get("models", [])}
            if model in existing:
                logger.info("Model %r already present, skipping pull", model)
                return
        except urllib.error.URLError as exc:
            logger.warning("Could not check /api/tags: %s", exc)

        logger.info("Pulling model %r", model)
        payload = json.dumps({"model": model, "stream": False}).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/pull",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            resp.read()

    def flush_model(self, model: str) -> None:
        """Unload model from memory via POST /api/generate with keep_alive=0."""
        payload = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

    def get_ps_status(self, model_id: str) -> ModelStatus | None:
        """Return status of model_id from /api/ps, or None if not loaded."""
        req = urllib.request.Request(f"{self._base_url}/api/ps")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for m in data.get("models", []):
            if m["name"] == model_id:
                return ModelStatus(
                    name=m["name"],
                    size=m.get("size", 0),
                    size_vram=m.get("size_vram", 0),
                    processor_status=m.get("processor", "unknown"),
                    expires_at=m.get("expires_at", ""),
                )
        return None
