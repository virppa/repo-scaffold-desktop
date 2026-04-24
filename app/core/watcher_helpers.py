"""Pure helper functions for the watcher sub-system (no I/O, unit-testable).

All functions in this module are stateless and have no self-dependencies.
This module may import from watcher_types only (no other watcher siblings).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import IO

from app.core.manifest import ExecutionManifest
from app.core.watcher_types import (
    _ENV_VARS_TO_STRIP_FOR_CLOUD,
    _LITELLM_BASE_URL,
    _LOCAL_MODEL,
    ActiveWorker,
)

# ---------------------------------------------------------------------------
# Worker log parsing
# ---------------------------------------------------------------------------


def _parse_worker_usage(log_path: Path) -> tuple[int | None, int | None]:
    """Read stream-json worker log and return (local_tokens, context_compactions)."""
    try:
        with log_path.open(encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "result":
                    usage = obj.get("usage") or {}
                    local_tokens = (usage.get("input_tokens") or 0) + (
                        usage.get("output_tokens") or 0
                    )
                    context_compactions = obj.get("context_compactions")
                    return local_tokens, context_compactions
    except Exception:
        return None, None
    return None, None


# ---------------------------------------------------------------------------
# Escalation-policy flag names (also used by watcher.py orchestrator)
# ---------------------------------------------------------------------------

_POLICY_FLAGS = (
    "scope_drift",
    "forbidden_path_touched",
    "import_linter_violation",
    "security_blocker",
)


def _read_result_flags(result_path: Path) -> dict[str, bool]:
    """Load result.json and return the four escalation-policy boolean flags.

    Returns all-False defaults when the file is missing or malformed.
    """
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        return dict.fromkeys(_POLICY_FLAGS, False)
    return {f: bool(raw.get(f, False)) for f in _POLICY_FLAGS}


# ---------------------------------------------------------------------------
# Allowed-paths overlap check
# ---------------------------------------------------------------------------


def check_allowed_paths_overlap(
    active: list[ActiveWorker], candidate: ExecutionManifest
) -> list[str]:
    """Return identifiers of active workers whose allowed_paths overlap with candidate.

    Two manifests overlap when they share at least one allowed_path pattern.
    An empty allowed_paths list means "no restriction" — treated as overlap with
    everything to be safe.
    """
    if not candidate.allowed_paths:
        return [w.manifest.ticket_id for w in active]

    conflicts: list[str] = []
    candidate_set = set(candidate.allowed_paths)
    for worker in active:
        if not worker.manifest.allowed_paths or candidate_set & set(
            worker.manifest.allowed_paths
        ):
            conflicts.append(worker.manifest.ticket_id)
    return conflicts


# ---------------------------------------------------------------------------
# Worker environment and command builders
# ---------------------------------------------------------------------------


def build_worker_env(
    mode: str,
    base_env: dict[str, str],
) -> dict[str, str]:
    """Return a subprocess environment dict for the given worker mode.

    cloud   — strips ANTHROPIC_BASE_URL and related vars so the process routes
              to the real Anthropic API.
    local   — injects ANTHROPIC_BASE_URL pointing to the LiteLLM proxy and sets
              ANTHROPIC_API_KEY=sk-dummy if not already present (LiteLLM doesn't
              validate the key; this satisfies Claude Code's auth check).
    default — passes base_env unchanged.
    """
    env = dict(base_env)
    if mode == "cloud":
        for var in _ENV_VARS_TO_STRIP_FOR_CLOUD:
            env.pop(var, None)
    elif mode == "local":
        env["ANTHROPIC_BASE_URL"] = _LITELLM_BASE_URL
        env.setdefault("ANTHROPIC_API_KEY", "sk-dummy")
    return env


def build_worker_cmd(
    ticket_id: str,
    mode: str,
    worktree_path: Path,
    prompt: str | None = None,
    disallowed_tools: list[str] | None = None,
) -> list[str]:
    """Return the claude subprocess command list for the given mode.

    prompt — pre-expanded skill content; defaults to the /implement-ticket
    slash-command shortcut (requires commands to be loaded by Claude Code).
    In --bare mode the shortcut is unavailable, so callers should pass the
    expanded implement-ticket.md content with $ARGUMENTS substituted.

    disallowed_tools — list of tool-call patterns passed to --disallowed-tools
    (e.g. ["Read(*watcher.py)", "Read(*metrics.py)"]) to enforce context_snippets.
    """
    if prompt is None:
        prompt = f"/implement-ticket {ticket_id}"
    # --bare strips auto-memory, hooks, and CLAUDE.md auto-discovery, keeping
    # the system prompt lean. --add-dir re-adds the worktree CLAUDE.md.
    # --strict-mcp-config + empty config prevents the Linear HTTP MCP server
    # from blocking ~180s on OAuth in non-interactive mode.
    # NOTE: --bare also strips OAuth credential loading, so it must NOT be used
    # for cloud mode where the worker authenticates via OAuth (Claude Max).
    # Local mode uses a dummy API key via LiteLLM, so --bare is safe there.
    base = [
        "claude",
        "--dangerously-skip-permissions",
        "--add-dir",
        str(worktree_path),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--effort",
        "max",
        "--verbose",
        "--output-format",
        "stream-json",
    ]
    if mode == "local":
        base.insert(2, "--bare")
    if disallowed_tools:
        base += ["--disallowed-tools", ",".join(disallowed_tools)]
    if mode == "local":
        return base + ["--model", _LOCAL_MODEL, "-p", prompt]
    return base + ["-p", prompt]


def resolve_effective_mode(worker_mode: str, manifest_mode: str) -> str:
    """Return the effective implementation mode.

    worker_mode takes precedence when it is not 'default'.
    Falls back to manifest_mode ('local', 'cloud', or 'hybrid').
    Hybrid is treated as 'cloud' for subprocess purposes.
    """
    if worker_mode != "default":
        return worker_mode
    if manifest_mode == "hybrid":
        return "cloud"
    return manifest_mode


# ---------------------------------------------------------------------------
# Worker output tee (runs in a daemon thread)
# ---------------------------------------------------------------------------


def _tee_worker_output(
    pipe: IO[bytes],
    log_file: IO[bytes],
    prefix: bytes,
    dest: IO[bytes],
) -> None:
    """Read *pipe* line-by-line, writing each line to *log_file* and *dest*.

    Runs in a daemon thread; returns when the pipe reaches EOF (worker exit).
    Closes *log_file* in the finally block — ownership transfers from the
    caller to this thread in verbose mode.
    """
    try:
        for raw_line in pipe:
            log_file.write(raw_line)
            log_file.flush()
            dest.write(prefix + raw_line)
            dest.flush()
    finally:
        log_file.close()


# ---------------------------------------------------------------------------
# Ollama config parsing
# ---------------------------------------------------------------------------


def _parse_ollama_model(config_path: Path) -> str:
    """Return the bare Ollama model name from a LiteLLM YAML config.

    Scans for the first 'model: ollama_chat/<name>' line and returns <name>.
    Raises ValueError if none is found, FileNotFoundError if the file is absent.
    """
    if not config_path.exists():
        raise FileNotFoundError(
            f"LiteLLM config not found: {config_path}. "
            "Copy litellm-local.yaml.example to litellm-local.yaml and configure it."
        )
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r"model:\s+ollama_chat/(\S+)", text)
    if match is None:
        raise ValueError(
            f"No ollama_chat/ model found in {config_path}. "
            "Add a model_list entry with litellm_params.model = 'ollama_chat/<model>'."
        )
    return match.group(1)
