"""vLLM health gating for the watcher sub-system.

Before WOR-368 this module managed two long-running daemon processes
(LiteLLM proxy + Ollama). Post-migration vLLM serves the Anthropic
Messages API natively (see docs/spikes/wor-344-vllm-native-anthropic-api.md),
so the watcher no longer spawns or stops any subprocesses — it only
gates dispatch on vLLM readiness. The class shape is preserved (rather
than collapsing to module functions) because the dispatch path holds a
reference to a ServiceManager instance and we don't want to ripple a
refactor through that surface in this ticket.
"""

from __future__ import annotations

import http.client
import json
import logging
import subprocess  # nosec B404
import sys
from pathlib import Path

from .watcher_types import _VLLM_HOST, _VLLM_PORT, _VLLM_SERVED_MODEL

logger = logging.getLogger(__name__)

# WOR-415: preserve_thinking via per-user-cache indirection. WOR-408 tried
# to add --default-chat-template-kwargs '{"preserve_thinking": true}' directly
# to the auto-start command but the JSON literal was mangled by the
# multi-shell quoting chain (subprocess.list2cmdline → wt.exe → wsl → bash).
# We now write the kwargs JSON inside WSL via `bash -c "tee …"` (stdin-piped,
# no shell quoting of the JSON content), and have the inner-most bash read
# it via $(cat …) at vLLM startup. The cat substitution happens inside the
# spawned bash where it's safe. Path is under WSL ~/.cache/ rather than /tmp
# to avoid shared-tmpdir hazards (bandit B108) — no symlink-attack surface
# on a per-user XDG cache path.
_VLLM_KWARGS_PATH = "~/.cache/repo-scaffold/qwen3_chat_template_kwargs.json"
_VLLM_KWARGS_JSON = '{"preserve_thinking": true}'

_VLLM_FP8_CMD = (
    "/home/antti/vllm-env/bin/vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4"
    " --served-model-name qwen3-coder"
    " --max-model-len 262144 --max-num-seqs 16"
    " --kv-cache-dtype fp8 --max-num-batched-tokens 4096"
    " --reasoning-parser qwen3 --enable-prefix-caching"
    " --language-model-only --safetensors-load-strategy prefetch"
    " --enable-auto-tool-choice --tool-call-parser qwen3_coder"
    f' --default-chat-template-kwargs "$(cat {_VLLM_KWARGS_PATH})"'
)


def _write_vllm_kwargs_file() -> None:
    """Write ``_VLLM_KWARGS_JSON`` to ``_VLLM_KWARGS_PATH`` inside WSL.

    Pipes the JSON via stdin into a `bash -c "mkdir -p … && tee …"` invocation
    so (a) the cache directory is created if missing and (b) the JSON content
    never appears in any shell command line — only the path does, and that
    has no special characters. Avoids the quoting chain that defeated
    WOR-408. The auto-start command's ``$(cat …)`` substitution reads this
    file at vLLM startup.

    On failure (wsl.exe missing, tee error, timeout) we log a warning and
    continue — the terminal will still open and vLLM will fail with a clear
    "no such file" error from cat, which the operator can fix manually.
    """
    bash_cmd = f"mkdir -p $(dirname {_VLLM_KWARGS_PATH}) && tee {_VLLM_KWARGS_PATH}"
    try:
        subprocess.run(  # nosec B603 B607
            ["wsl", "bash", "-c", bash_cmd],
            input=_VLLM_KWARGS_JSON.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
        )
        logger.debug(
            "Wrote vLLM chat-template kwargs to %s (preserve_thinking=true)",
            _VLLM_KWARGS_PATH,
        )
    except FileNotFoundError:
        logger.warning("wsl.exe not found — vLLM will start without preserve_thinking")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Could not write %s — vLLM may start without preserve_thinking: %s",
            _VLLM_KWARGS_PATH,
            exc,
        )


class ServiceManager:
    """vLLM health gate for local-mode workers.

    The class previously also owned LiteLLM and Ollama process handles;
    those backends were retired in WOR-368 once vLLM 0.20.0 began serving
    /v1/messages natively. ``stop()`` is a no-op kept for call-site
    compatibility with the watcher's signal handler.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._running = True
        self._vllm_terminal_opened = False
        self._vllm_warned = False

    def probe_vllm_health(self) -> bool:
        """Check whether vLLM is ready to serve on localhost:_VLLM_PORT.

        Uses /v1/models (not /health): /health returns 200 as soon as the HTTP
        server starts, before model weights are loaded. /v1/models only returns
        200 once the model is registered and ready for inference.

        Returns True if ready. Logs at WARNING level when not ready; the full
        startup command is printed only on the first failure to avoid log spam.
        On Windows opens a new WSL2 terminal tab on the first failure only.
        """
        try:
            conn = http.client.HTTPConnection(_VLLM_HOST, _VLLM_PORT, timeout=3)
            conn.request("GET", "/v1/models")
            resp = conn.getresponse()
            if resp.status == 200:
                logger.debug("vLLM ready (port %d)", _VLLM_PORT)
                return True
        except (OSError, http.client.HTTPException):
            pass

        if not self._vllm_warned:
            logger.warning(
                "vLLM not responding on port %d — start the server in WSL2:\n\n  %s\n",
                _VLLM_PORT,
                _VLLM_FP8_CMD,
            )
            self._vllm_warned = True
        else:
            logger.warning("vLLM not ready yet on port %d — waiting…", _VLLM_PORT)

        if sys.platform == "win32" and not self._vllm_terminal_opened:
            self._open_vllm_terminal()
            self._vllm_terminal_opened = True
        return False

    def _open_vllm_terminal(self) -> None:
        """Open a new Windows Terminal tab running the vLLM FP8 command in WSL2.

        Writes the chat-template kwargs JSON to ``_VLLM_KWARGS_PATH`` first so
        the auto-start command's ``$(cat …)`` substitution finds it (WOR-415).
        If the kwargs write fails the terminal still opens — vLLM will then
        fail to start with a clear "no such file" error from cat, which the
        operator can fix manually.
        """
        _write_vllm_kwargs_file()
        try:
            subprocess.Popen(  # nosec B603 B607
                [
                    "wt.exe",
                    "-w",
                    "0",
                    "new-tab",
                    "--",
                    "wsl",
                    "bash",
                    "-i",
                    "-c",
                    _VLLM_FP8_CMD,
                ],
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
            logger.info("Opened WSL2 terminal tab for vLLM server")
        except FileNotFoundError:
            logger.warning("wt.exe not found — start vLLM manually in WSL2")
        except OSError as exc:
            logger.warning("Could not open WSL2 terminal: %s", exc)

    def ensure_vllm_anthropic_mode(self) -> None:
        """Verify vLLM is serving the Anthropic Messages API natively.

        Probes two endpoints:
            1. GET  /v1/models   — confirms the served model name is registered
            2. POST /v1/messages — confirms the Anthropic router accepts traffic

        Raises RuntimeError with the launch command embedded if either probe
        fails. Local-mode dispatch must call this before launching a worker;
        otherwise the worker's first claude API call would hang or fail in a
        less obvious way (the worker's stdout is tee'd to a log file but the
        operator only sees opaque errors).

        This replaces ensure_litellm_running (deleted in WOR-368): vLLM 0.20.0
        mounts /v1/messages unconditionally on the OpenAI server, so we no
        longer spawn a translation proxy — we just verify the destination is
        live and producing Anthropic-shaped responses.
        """
        if not self._probe_models_endpoint():
            raise RuntimeError(
                f"vLLM not serving on port {_VLLM_PORT}. Start it in WSL2:\n\n"
                f"  {_VLLM_FP8_CMD}\n"
            )
        if not self._probe_messages_endpoint():
            raise RuntimeError(
                f"vLLM /v1/messages on port {_VLLM_PORT} did not return a "
                "valid Anthropic response. The server is up but the Anthropic "
                "router is not responding correctly — check the vLLM version "
                "(needs 0.20.0+) and that --enable-auto-tool-choice + "
                "--tool-call-parser qwen3_coder are set."
            )
        logger.debug("vLLM Anthropic mode verified (port %d)", _VLLM_PORT)

    def _probe_models_endpoint(self) -> bool:
        """Return True if GET /v1/models returns 200 and lists _VLLM_SERVED_MODEL."""
        try:
            conn = http.client.HTTPConnection(_VLLM_HOST, _VLLM_PORT, timeout=3)
            conn.request("GET", "/v1/models")
            resp = conn.getresponse()
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
        except (OSError, http.client.HTTPException, ValueError):
            return False
        ids = [m.get("id") for m in payload.get("data", [])]
        return _VLLM_SERVED_MODEL in ids

    def _probe_messages_endpoint(self) -> bool:
        """Return True if POST /v1/messages returns a valid Anthropic message."""
        body = json.dumps(
            {
                "model": _VLLM_SERVED_MODEL,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "ok"}],
            }
        ).encode("utf-8")
        try:
            conn = http.client.HTTPConnection(_VLLM_HOST, _VLLM_PORT, timeout=10)
            conn.request(
                "POST",
                "/v1/messages",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": "dummy",
                },
            )
            resp = conn.getresponse()
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
        except (OSError, http.client.HTTPException, ValueError):
            return False
        return payload.get("type") == "message" and "content" in payload

    def stop(self) -> None:
        """No-op kept for call-site compatibility (formerly terminated LiteLLM)."""
        self._running = False
