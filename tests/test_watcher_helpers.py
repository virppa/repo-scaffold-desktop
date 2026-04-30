"""Tests for app.core.watcher_helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.watcher_helpers import (
    _parse_ollama_model,
    _parse_worker_usage,
    build_worker_cmd,
    build_worker_env,
    check_allowed_paths_overlap,
    resolve_effective_mode,
)
from tests.conftest import make_active_worker, make_manifest

# ---------------------------------------------------------------------------
# check_allowed_paths_overlap
# ---------------------------------------------------------------------------


def test_overlap_when_paths_share_entry() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=["app/core/foo.py"])]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_no_overlap_when_paths_are_disjoint() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=["app/core/bar.py"])]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == []


def test_empty_candidate_paths_conflicts_with_all() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=["app/core/bar.py"])]
    candidate = make_manifest(allowed_paths=[])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_empty_active_paths_conflicts_with_candidate() -> None:
    active = [make_active_worker("WOR-11", allowed_paths=[])]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


def test_multiple_active_partial_overlap() -> None:
    active = [
        make_active_worker("WOR-11", allowed_paths=["app/core/foo.py"]),
        make_active_worker("WOR-12", allowed_paths=["app/core/baz.py"]),
    ]
    candidate = make_manifest(allowed_paths=["app/core/foo.py"])
    assert check_allowed_paths_overlap(active, candidate) == ["WOR-11"]


# ---------------------------------------------------------------------------
# build_worker_env
# ---------------------------------------------------------------------------


def test_cloud_mode_strips_base_url() -> None:
    base = {
        "ANTHROPIC_BASE_URL": "http://localhost:8082",
        "PATH": "/usr/bin",
        "HOME": "/root",
    }
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["PATH"] == "/usr/bin"


def test_cloud_mode_strips_model_var() -> None:
    base = {"ANTHROPIC_MODEL": "qwen3-coder:30b", "PATH": "/usr/bin"}
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_MODEL" not in env


def test_local_mode_injects_base_url() -> None:
    base = {"PATH": "/usr/bin"}
    env = build_worker_env("local", base)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8082"


def test_default_mode_passes_env_unchanged() -> None:
    base = {"ANTHROPIC_BASE_URL": "http://localhost:8082", "PATH": "/usr/bin"}
    env = build_worker_env("default", base)
    assert env == base


def test_cloud_mode_does_not_inject_base_url_if_absent() -> None:
    base = {"PATH": "/usr/bin"}
    env = build_worker_env("cloud", base)
    assert "ANTHROPIC_BASE_URL" not in env


# ---------------------------------------------------------------------------
# build_worker_cmd
# ---------------------------------------------------------------------------


def test_cloud_cmd_has_no_model_flag(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path)
    assert "--model" not in cmd
    assert "/implement-ticket WOR-10" in " ".join(cmd)


def test_local_cmd_includes_model_flag(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path)
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "claude-sonnet-4-6"


def test_cmd_includes_dangerously_skip_permissions(tmp_path: Path) -> None:
    for mode in ("cloud", "local"):
        cmd = build_worker_cmd("WOR-10", mode, tmp_path)
        assert "--dangerously-skip-permissions" in cmd


def test_cmd_bare_mode_uses_worktree_path(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path)
    assert "--bare" in cmd
    idx = cmd.index("--add-dir")
    assert cmd[idx + 1] == str(tmp_path)


def test_cloud_cmd_has_no_bare_flag(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path)
    assert "--bare" not in cmd


def test_cmd_disallowed_tools_appended(tmp_path: Path) -> None:
    tools = ["Read(*watcher.py)", "Read(*metrics.py)"]
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=tools)
    assert "--disallowed-tools" in cmd
    idx = cmd.index("--disallowed-tools")
    assert cmd[idx + 1] == "Read(*watcher.py),Read(*metrics.py)"


def test_cmd_no_disallowed_tools_when_none(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=None)
    assert "--disallowed-tools" not in cmd


# ---------------------------------------------------------------------------
# resolve_effective_mode
# ---------------------------------------------------------------------------


def test_worker_mode_overrides_manifest_local() -> None:
    assert resolve_effective_mode("cloud", "local") == "cloud"


def test_worker_mode_overrides_manifest_cloud() -> None:
    assert resolve_effective_mode("local", "cloud") == "local"


def test_default_defers_to_manifest() -> None:
    assert resolve_effective_mode("default", "local") == "local"
    assert resolve_effective_mode("default", "cloud") == "cloud"


def test_default_hybrid_becomes_cloud() -> None:
    assert resolve_effective_mode("default", "hybrid") == "cloud"


# ---------------------------------------------------------------------------
# _parse_worker_usage
# ---------------------------------------------------------------------------


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "worker_wor-99.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def test_parse_worker_usage_success(tmp_path: Path) -> None:
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
            },
            "context_compactions": 3,
        }
    )
    log = _write_log(tmp_path, ['{"type":"other","x":1}', result_line])
    input_tok, output_tok, compactions = _parse_worker_usage(log)
    assert input_tok == 1000
    assert output_tok == 200
    assert compactions == 3


def test_parse_worker_usage_no_context_compactions(tmp_path: Path) -> None:
    result_line = json.dumps(
        {"type": "result", "usage": {"input_tokens": 500, "output_tokens": 50}}
    )
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, compactions = _parse_worker_usage(log)
    assert input_tok == 500
    assert output_tok == 50
    assert compactions is None


def test_parse_worker_usage_missing_log(tmp_path: Path) -> None:
    log = tmp_path / "no_such_file.log"
    input_tok, output_tok, compactions = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions is None


def test_parse_worker_usage_no_result_line(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "tool_use", "name": "Bash"}),
            json.dumps({"type": "assistant", "content": "hello"}),
        ],
    )
    input_tok, output_tok, compactions = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions is None


def test_parse_worker_usage_malformed_json(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.write_text("not json at all\n{broken\n", encoding="utf-8")
    input_tok, output_tok, compactions = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions is None


def test_parse_worker_usage_mixed_valid_invalid_lines(tmp_path: Path) -> None:
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 300, "output_tokens": 100},
            "context_compactions": 1,
        }
    )
    log = tmp_path / "worker.log"
    log.write_text("garbage line\n" + result_line + "\n", encoding="utf-8")
    input_tok, output_tok, compactions = _parse_worker_usage(log)
    assert input_tok == 300
    assert output_tok == 100
    assert compactions == 1


def test_parse_worker_usage_returns_first_result_line(tmp_path: Path) -> None:
    first = json.dumps(
        {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    second = json.dumps(
        {"type": "result", "usage": {"input_tokens": 999, "output_tokens": 999}}
    )
    log = _write_log(tmp_path, [first, second])
    input_tok, output_tok, _ = _parse_worker_usage(log)
    assert input_tok == 10
    assert output_tok == 5


def test_parse_worker_usage_empty_file(tmp_path: Path) -> None:
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    input_tok, output_tok, compactions = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions is None


# ---------------------------------------------------------------------------
# _parse_ollama_model
# ---------------------------------------------------------------------------


def test_parse_ollama_model_returns_bare_model_name(tmp_path: Path) -> None:
    cfg = tmp_path / "litellm-local.yaml"
    cfg.write_text(
        "model_list:\n"
        "  - model_name: claude-sonnet-4-6\n"
        "    litellm_params:\n"
        "      model: ollama_chat/qwen3-coder:30b\n"
        "      api_base: http://localhost:11434\n"
    )
    assert _parse_ollama_model(cfg) == "qwen3-coder:30b"


def test_parse_ollama_model_raises_when_no_ollama_entry(tmp_path: Path) -> None:
    import pytest

    cfg = tmp_path / "litellm-local.yaml"
    cfg.write_text("model_list:\n  - model_name: gpt-4\n")
    with pytest.raises(ValueError, match="No ollama_chat/"):
        _parse_ollama_model(cfg)


def test_parse_ollama_model_raises_when_file_missing(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        _parse_ollama_model(tmp_path / "nonexistent.yaml")


# ---------------------------------------------------------------------------
# _parse_worker_usage — 3-tuple return (WOR-230)
# ---------------------------------------------------------------------------


def test_parse_worker_usage_returns_separate_tokens(tmp_path: Path) -> None:
    """input_tokens and output_tokens are returned separately."""
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 12000, "output_tokens": 800},
        }
    )
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _ = _parse_worker_usage(log)
    assert input_tok == 12000
    assert output_tok == 800


def test_parse_worker_usage_missing_input_token_returns_none(
    tmp_path: Path,
) -> None:
    """When input_tokens is absent, all tokens are None."""
    result_line = json.dumps({"type": "result", "usage": {"output_tokens": 500}})
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None


def test_parse_worker_usage_missing_output_token_returns_none(
    tmp_path: Path,
) -> None:
    """When output_tokens is absent, all tokens are None."""
    result_line = json.dumps({"type": "result", "usage": {"input_tokens": 3000}})
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
