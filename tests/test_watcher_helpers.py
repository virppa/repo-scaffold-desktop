"""Tests for app.core.watcher_helpers."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.watcher.watcher_helpers import (
    _parse_ollama_model,
    _parse_worker_api_retries,
    _parse_worker_subagent_spawns,
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


def test_local_cmd_uses_worktree_path(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path)
    assert "--bare" not in cmd
    idx = cmd.index("--add-dir")
    assert cmd[idx + 1] == str(tmp_path)


def test_no_bare_flag_in_any_mode(tmp_path: Path) -> None:
    for mode in ("cloud", "local"):
        cmd = build_worker_cmd("WOR-10", mode, tmp_path)
        assert "--bare" not in cmd, f"--bare should not appear in {mode} mode"


def test_cmd_disallowed_tools_appended(tmp_path: Path) -> None:
    tools = ["Read(*watcher.py)", "Read(*metrics.py)"]
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=tools)
    assert "--disallowed-tools" in cmd
    idx = cmd.index("--disallowed-tools")
    assert cmd[idx + 1] == "Read(*watcher.py),Read(*metrics.py)"


def test_cmd_no_disallowed_tools_when_none(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, disallowed_tools=None)
    assert "--disallowed-tools" not in cmd


def test_cmd_uses_empty_mcp_config_by_default(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path)
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == '{"mcpServers":{}}'


def test_cmd_uses_custom_mcp_config_when_provided(tmp_path: Path) -> None:
    config = '{"mcpServers":{"linear-server":{"type":"http","url":"https://mcp.linear.app/mcp"}}}'
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, mcp_config_json=config)
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == config


# ---------------------------------------------------------------------------
# build_worker_cmd — effort (WOR-214)
# ---------------------------------------------------------------------------


def test_build_worker_cmd_with_explicit_effort(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path, effort="high")
    assert "--effort" in cmd
    idx = cmd.index("--effort")
    assert cmd[idx + 1] == "high"


def test_build_worker_cmd_effort_none_local_falls_back_to_xhigh(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "local", tmp_path, effort=None)
    assert "--effort" in cmd
    idx = cmd.index("--effort")
    assert cmd[idx + 1] == "xhigh"


def test_build_worker_cmd_effort_none_cloud_falls_back_to_max(tmp_path: Path) -> None:
    cmd = build_worker_cmd("WOR-10", "cloud", tmp_path, effort=None)
    assert "--effort" in cmd
    idx = cmd.index("--effort")
    assert cmd[idx + 1] == "max"


def test_build_worker_cmd_explicit_effort_ignored_by_mode(tmp_path: Path) -> None:
    """Explicit effort value is used regardless of mode."""
    for mode in ("local", "cloud"):
        cmd = build_worker_cmd("WOR-10", mode, tmp_path, effort="low")
        assert "--effort" in cmd
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "low"


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
    """Result event provides token snapshot; no compact_boundary → 0 (WOR-357).

    The pre-WOR-357 implementation read context_compactions from the result
    event's top-level field — that field is never populated by Claude Code.
    The test now reflects the new behaviour: compactions counted only from
    type=system, subtype=compact_boundary events.
    """
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 0,
            },
            "context_compactions": 3,  # ignored under WOR-357 semantics
        }
    )
    log = _write_log(tmp_path, ['{"type":"other","x":1}', result_line])
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 1000
    assert output_tok == 200
    assert compactions == 0  # was 3 under the old (broken) semantics


def test_parse_worker_usage_no_context_compactions(tmp_path: Path) -> None:
    """Parseable log with no compact_boundary events returns 0 (WOR-357)."""
    result_line = json.dumps(
        {"type": "result", "usage": {"input_tokens": 500, "output_tokens": 50}}
    )
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 500
    assert output_tok == 50
    assert compactions == 0  # was None under the old semantics


def test_parse_worker_usage_missing_log(tmp_path: Path) -> None:
    """Missing log returns None for all three fields (unchanged)."""
    log = tmp_path / "no_such_file.log"
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions is None


def test_parse_worker_usage_no_result_line(tmp_path: Path) -> None:
    """Parseable log without usable usage data returns 0 compactions (WOR-357)."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "tool_use", "name": "Bash"}),
            json.dumps({"type": "assistant", "content": "hello"}),
        ],
    )
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions == 0  # log was parseable; just no compactions


def test_parse_worker_usage_malformed_json(tmp_path: Path) -> None:
    """Fully unparseable log returns None for all three fields (unchanged)."""
    log = tmp_path / "worker.log"
    log.write_text("not json at all\n{broken\n", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    # No JSON parseable at all → 0 events seen, but the file IS open-able,
    # so we get the parseable-but-empty path.
    assert compactions == 0


# ---------------------------------------------------------------------------
# WOR-357 — compact_boundary system event counting
# ---------------------------------------------------------------------------


def test_parse_worker_usage_one_compact_boundary(tmp_path: Path) -> None:
    """A single compact_boundary system event yields context_compactions=1."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {
                        "trigger": "auto",
                        "pre_tokens": 135486,
                        "post_tokens": 3348,
                        "duration_ms": 88463,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            ),
        ],
    )
    _, _, compactions, _ = _parse_worker_usage(log)
    assert compactions == 1


def test_parse_worker_usage_multiple_compact_boundaries(tmp_path: Path) -> None:
    """Three compact_boundary events sum to context_compactions=3."""
    boundary = json.dumps(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {"trigger": "auto"},
        }
    )
    log = _write_log(
        tmp_path,
        [
            boundary,
            json.dumps({"type": "assistant", "message": {"id": "a1"}}),
            boundary,
            json.dumps({"type": "assistant", "message": {"id": "a2"}}),
            boundary,
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            ),
        ],
    )
    _, _, compactions, _ = _parse_worker_usage(log)
    assert compactions == 3


def test_parse_worker_usage_compact_duration_summed(tmp_path: Path) -> None:
    """WOR-358: 4th tuple element sums compact_metadata.duration_ms."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"duration_ms": 50000},
                }
            ),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"duration_ms": 38463},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 5},
                }
            ),
        ],
    )
    _, _, compactions, compact_dur = _parse_worker_usage(log)
    assert compactions == 2
    assert compact_dur == 88463


def test_parse_worker_usage_compact_duration_zero_when_no_compactions(
    tmp_path: Path,
) -> None:
    """No compact_boundary events → compact_duration_ms is 0, not None."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    _, _, compactions, compact_dur = _parse_worker_usage(log)
    assert compactions == 0
    assert compact_dur == 0


def test_parse_worker_usage_compact_duration_missing_metadata(tmp_path: Path) -> None:
    """compact_boundary event without duration_ms → counts as compaction but adds 0."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"trigger": "auto"},  # no duration_ms key
                }
            ),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    _, _, compactions, compact_dur = _parse_worker_usage(log)
    assert compactions == 2
    assert compact_dur == 0


def test_parse_worker_usage_other_system_subtypes_ignored(tmp_path: Path) -> None:
    """system events with non-compact_boundary subtypes do not increment."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "system", "subtype": "task_started"}),
            json.dumps({"type": "system", "subtype": "api_retry"}),
            json.dumps({"type": "system", "subtype": "task_notification"}),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 100, "output_tokens": 5},
                }
            ),
        ],
    )
    _, _, compactions, _ = _parse_worker_usage(log)
    assert compactions == 0


def test_parse_worker_usage_compact_boundary_with_assistant_usage(
    tmp_path: Path,
) -> None:
    """Cumulative assistant-token sum AND compaction count both reported."""
    log = _write_log(
        tmp_path,
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "a1",
                        "usage": {"input_tokens": 1000, "output_tokens": 100},
                    },
                }
            ),
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "a2",
                        "usage": {"input_tokens": 500, "output_tokens": 50},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "usage": {"input_tokens": 1500, "output_tokens": 150},
                }
            ),
        ],
    )
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 1500  # 1000 + 500 (sum across assistant events)
    assert output_tok == 150  # 100 + 50
    assert compactions == 1


def test_parse_worker_usage_cumulative_output_tokens(tmp_path: Path) -> None:
    """output_tokens summed across every type=assistant event."""
    assistant = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a1",
                    "usage": {
                        "input_tokens": 10000,
                        "output_tokens": 100,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a2",
                    "usage": {
                        "input_tokens": 10000,
                        "output_tokens": 200,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a3",
                    "usage": {
                        "input_tokens": 10000,
                        "output_tokens": 300,
                    },
                },
            }
        ),
    ]
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {
                "input_tokens": 40000,
                "output_tokens": 707,
            },
            "context_compactions": 5,
        }
    )
    log = tmp_path / "worker.log"
    log.write_text("\n".join(assistant + [result_line]) + "\n", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert output_tok == 600  # 100+200+300
    assert compactions == 0  # WOR-357: result.context_compactions field is ignored


def test_parse_worker_usage_cumulative_input_tokens(tmp_path: Path) -> None:
    """input_tokens summed across every type=assistant event."""
    assistant = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a1",
                    "usage": {
                        "input_tokens": 8000,
                        "output_tokens": 50,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a2",
                    "usage": {
                        "input_tokens": 12000,
                        "output_tokens": 60,
                    },
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "a3",
                    "usage": {
                        "input_tokens": 15000,
                        "output_tokens": 70,
                    },
                },
            }
        ),
    ]
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {
                "input_tokens": 50000,
                "output_tokens": 200,
            },
            "context_compactions": 2,
        }
    )
    log = tmp_path / "worker.log"
    log.write_text("\n".join(assistant + [result_line]) + "\n", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 35000  # 8000+12000+15000


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
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok == 300
    assert output_tok == 100
    assert compactions == 0  # WOR-357: result.context_compactions field is ignored


def test_parse_worker_usage_returns_first_result_line(tmp_path: Path) -> None:
    first = json.dumps(
        {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    second = json.dumps(
        {"type": "result", "usage": {"input_tokens": 999, "output_tokens": 999}}
    )
    log = _write_log(tmp_path, [first, second])
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    # No assistant events — fallback uses the last result event's snapshot.
    assert input_tok == 999
    assert output_tok == 999


def test_parse_worker_usage_empty_file(tmp_path: Path) -> None:
    """Empty file is open-able and parseable → (None, None, 0)."""
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    input_tok, output_tok, compactions, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
    assert compactions == 0  # WOR-357: parseable path returns 0, not None


# ---------------------------------------------------------------------------
# WOR-360 — _parse_worker_api_retries
# ---------------------------------------------------------------------------


def test_parse_worker_api_retries_zero(tmp_path: Path) -> None:
    """Log with no api_retry events returns 0."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_api_retries(log) == 0


def test_parse_worker_api_retries_counts_5(tmp_path: Path) -> None:
    """5 api_retry events returns 5."""
    retry = json.dumps({"type": "system", "subtype": "api_retry"})
    log = _write_log(
        tmp_path,
        [
            retry,
            retry,
            retry,
            retry,
            retry,
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_api_retries(log) == 5


def test_parse_worker_api_retries_other_subtypes_ignored(tmp_path: Path) -> None:
    """Other system subtypes (init, compact_boundary, task_started) ignored."""
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "system", "subtype": "compact_boundary"}),
            json.dumps({"type": "system", "subtype": "task_started"}),
            json.dumps({"type": "system", "subtype": "task_notification"}),
            json.dumps({"type": "system", "subtype": "api_retry"}),
        ],
    )
    assert _parse_worker_api_retries(log) == 1


def test_parse_worker_api_retries_missing_log(tmp_path: Path) -> None:
    """Missing log returns None (cannot read)."""
    assert _parse_worker_api_retries(tmp_path / "no_such_file.log") is None


# ---------------------------------------------------------------------------
# WOR-364 — _parse_worker_subagent_spawns
# ---------------------------------------------------------------------------


def _task_use(name: str = "Task") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "a1",
                "content": [{"type": "tool_use", "name": name, "input": {}}],
            },
        }
    )


def test_parse_worker_subagent_spawns_zero(tmp_path: Path) -> None:
    """Log with no Task tool_use returns 0."""
    log = _write_log(
        tmp_path,
        [
            _task_use("Read"),
            _task_use("Edit"),
            _task_use("Bash"),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_subagent_spawns(log) == 0


def test_parse_worker_subagent_spawns_counts_3(tmp_path: Path) -> None:
    """3 Task tool_use events returns 3."""
    log = _write_log(
        tmp_path,
        [
            _task_use("Task"),
            _task_use("Read"),
            _task_use("Task"),
            _task_use("Bash"),
            _task_use("Task"),
            json.dumps(
                {"type": "result", "usage": {"input_tokens": 100, "output_tokens": 5}}
            ),
        ],
    )
    assert _parse_worker_subagent_spawns(log) == 3


def test_parse_worker_subagent_spawns_other_tools_ignored(tmp_path: Path) -> None:
    """Read/Edit/Bash/Grep/Write/TodoWrite are not counted."""
    log = _write_log(
        tmp_path,
        [
            _task_use("Read"),
            _task_use("Edit"),
            _task_use("Bash"),
            _task_use("Grep"),
            _task_use("Write"),
            _task_use("TodoWrite"),
        ],
    )
    assert _parse_worker_subagent_spawns(log) == 0


def test_parse_worker_subagent_spawns_missing_log(tmp_path: Path) -> None:
    """Missing log returns None."""
    assert _parse_worker_subagent_spawns(tmp_path / "no_such_file.log") is None


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
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    assert input_tok == 12000
    assert output_tok == 800


def test_parse_worker_usage_missing_input_token_returns_none(
    tmp_path: Path,
) -> None:
    """When input_tokens is absent, all tokens are None."""
    result_line = json.dumps({"type": "result", "usage": {"output_tokens": 500}})
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None


def test_parse_worker_usage_missing_output_token_returns_none(
    tmp_path: Path,
) -> None:
    """When output_tokens is absent, all tokens are None."""
    result_line = json.dumps({"type": "result", "usage": {"input_tokens": 3000}})
    log = _write_log(tmp_path, [result_line])
    input_tok, output_tok, _, _ = _parse_worker_usage(log)
    assert input_tok is None
    assert output_tok is None
