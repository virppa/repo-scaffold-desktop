"""LiteLLM and Ollama process management for the watcher sub-system.

ServiceManager owns the shared _litellm_proc handle; two methods depend on it
(_ensure_litellm_running stores it, _stop_litellm_proxy uses it) which makes a
class boundary cleaner than threading a Popen handle through function signatures.
"""

from __future__ import annotations

import http.client
import logging
import os
import socket
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

from app.core.watcher_helpers import _parse_ollama_model
from app.core.watcher_types import (
    _LITELLM_CONFIG,
    _LITELLM_PORT,
    _OLLAMA_KEEPALIVE,
    _OLLAMA_PORT,
)

logger = logging.getLogger(__name__)


class ServiceManager:
    """Manages the LiteLLM proxy and Ollama processes for local-mode workers."""

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._litellm_proc: subprocess.Popen[bytes] | None = None

    def ensure_ollama_running(self) -> None:
        """Start Ollama with the configured model if not already on _OLLAMA_PORT."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            already_up = sock.connect_ex(("localhost", _OLLAMA_PORT)) == 0

        if already_up:
            logger.info("Ollama already running on port %d", _OLLAMA_PORT)
            return

        config_path = self._repo_root / _LITELLM_CONFIG
        model = _parse_ollama_model(config_path)
        logger.info(
            "Starting Ollama (model=%s, keepalive=%s)…", model, _OLLAMA_KEEPALIVE
        )
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NEW_CONSOLE
        else:
            creation_flags = 0
        subprocess.Popen(  # nosec B603 B607
            ["ollama", "run", model, "--keepalive", _OLLAMA_KEEPALIVE],
            creationflags=creation_flags,
        )
        self._wait_for_ollama_ready()

    def _wait_for_ollama_ready(self, timeout: float = 120.0) -> None:
        """Poll TCP then HTTP /api/tags until Ollama's API is ready."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                if sock.connect_ex(("localhost", _OLLAMA_PORT)) != 0:
                    time.sleep(0.5)
                    continue
            try:
                conn = http.client.HTTPConnection("localhost", _OLLAMA_PORT, timeout=2)
                conn.request("GET", "/api/tags")
                if conn.getresponse().status == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.5)
        raise TimeoutError(f"Ollama not ready after {timeout}s.")

    def ensure_litellm_running(self) -> None:
        """Start the LiteLLM proxy if not already listening on _LITELLM_PORT."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            already_up = sock.connect_ex(("localhost", _LITELLM_PORT)) == 0

        if already_up:
            logger.info("LiteLLM proxy already running on port %d", _LITELLM_PORT)
            return

        config_path = self._repo_root / _LITELLM_CONFIG
        if not config_path.exists():
            raise FileNotFoundError(
                f"LiteLLM config not found: {config_path}. "
                "Copy litellm-local.yaml.example to litellm-local.yaml "
                "and configure it."
            )

        logger.info("Starting LiteLLM proxy (port %d)…", _LITELLM_PORT)
        env = {**os.environ, "PYTHONUTF8": "1"}
        if sys.platform == "win32":
            self._litellm_proc = subprocess.Popen(  # nosec B603 B607
                [
                    "litellm",
                    "--config",
                    str(config_path),
                    "--port",
                    str(_LITELLM_PORT),
                    "--drop_params",
                ],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                env=env,
            )
        else:
            log_path = self._repo_root / ".claude" / "litellm.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "wb")  # noqa: SIM115
            logger.info("LiteLLM log: %s", log_path)
            self._litellm_proc = subprocess.Popen(  # nosec B603 B607
                [
                    "litellm",
                    "--config",
                    str(config_path),
                    "--port",
                    str(_LITELLM_PORT),
                    "--drop_params",
                ],
                stdout=log_file,
                stderr=log_file,
                env=env,
            )
        self._wait_for_litellm_ready()

    def _wait_for_litellm_ready(self, timeout: float = 60.0) -> None:
        """Poll TCP until LiteLLM's port accepts connections or process dies."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._litellm_proc and self._litellm_proc.poll() is not None:
                rc = self._litellm_proc.returncode
                raise RuntimeError(
                    f"LiteLLM proxy exited (rc={rc}). "
                    f"Check .claude/litellm.log for details."
                )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                if sock.connect_ex(("localhost", _LITELLM_PORT)) == 0:
                    return
            time.sleep(0.5)
        raise TimeoutError(
            f"LiteLLM proxy not ready after {timeout}s. "
            f"Check .claude/litellm.log for details."
        )

    def stop(self) -> None:
        """Terminate the LiteLLM proxy if it was started by this manager."""
        if not self._litellm_proc:
            return
        logger.info("Stopping LiteLLM proxy (pid=%d)…", self._litellm_proc.pid)
        self._litellm_proc.terminate()
        try:
            self._litellm_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.info("LiteLLM proxy did not exit after 5s — sending kill")
            self._litellm_proc.kill()
        self._litellm_proc = None
