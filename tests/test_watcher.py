"""Tests for app.core.watcher — _parse_worker_usage and _finalize_worker integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.watcher import _parse_worker_usage

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
    tokens, compactions = _parse_worker_usage(log)
    assert tokens == 1200
    assert compactions == 3


def test_parse_worker_usage_no_context_compactions(tmp_path: Path) -> None:
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 500, "output_tokens": 50},
        }
    )
    log = _write_log(tmp_path, [result_line])
    tokens, compactions = _parse_worker_usage(log)
    assert tokens == 550
    assert compactions is None


def test_parse_worker_usage_missing_log(tmp_path: Path) -> None:
    tokens, compactions = _parse_worker_usage(tmp_path / "no_such_file.log")
    assert tokens is None
    assert compactions is None


def test_parse_worker_usage_no_result_line(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        [
            json.dumps({"type": "tool_use", "name": "Bash"}),
            json.dumps({"type": "assistant", "content": "hello"}),
        ],
    )
    tokens, compactions = _parse_worker_usage(log)
    assert tokens is None
    assert compactions is None


def test_parse_worker_usage_malformed_json(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.write_text("not json at all\n{broken\n", encoding="utf-8")
    tokens, compactions = _parse_worker_usage(log)
    assert tokens is None
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
    log.write_text(
        "garbage line\n" + result_line + "\n",
        encoding="utf-8",
    )
    tokens, compactions = _parse_worker_usage(log)
    assert tokens == 400
    assert compactions == 1


def test_parse_worker_usage_returns_first_result_line(tmp_path: Path) -> None:
    first = json.dumps(
        {"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    second = json.dumps(
        {"type": "result", "usage": {"input_tokens": 999, "output_tokens": 999}}
    )
    log = _write_log(tmp_path, [first, second])
    tokens, _ = _parse_worker_usage(log)
    assert tokens == 15


def test_parse_worker_usage_empty_file(tmp_path: Path) -> None:
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    tokens, compactions = _parse_worker_usage(log)
    assert tokens is None
    assert compactions is None


# ---------------------------------------------------------------------------
# _finalize_worker integration — local_tokens + context_compactions passed
# ---------------------------------------------------------------------------


def test_finalize_worker_passes_usage_to_metrics(tmp_path: Path) -> None:
    """_finalize_worker calls _parse_worker_usage and passes fields to TicketMetrics."""
    from app.core.watcher import Watcher

    manifest = MagicMock()
    manifest.failure_policy.escalate_to_cloud = False
    manifest.failure_policy.on_check_failure = "abort"
    manifest.epic_id = "WOR-115"
    manifest.implementation_mode = "local"
    manifest.ticket_state_map.failed = "Blocked"
    manifest.ticket_state_map.merged_to_epic = "MergedToEpic"

    ticket_id = "WOR-121"
    log_dir = tmp_path / ".claude"
    log_dir.mkdir(parents=True)
    log_file = log_dir / f"worker_{ticket_id.lower()}.log"
    result_line = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 2000, "output_tokens": 400},
            "context_compactions": 5,
        }
    )
    log_file.write_text(result_line + "\n", encoding="utf-8")

    worker = MagicMock()
    worker.ticket_id = ticket_id
    worker.linear_id = "abc123"
    worker.manifest = manifest
    worker.worktree_path = tmp_path
    worker.retry_count = 0
    worker.backed_up_plans = []

    recorded: list = []
    metrics_store = MagicMock()
    metrics_store.record.side_effect = lambda m: recorded.append(m)

    watcher = object.__new__(Watcher)
    watcher._metrics = metrics_store
    watcher._project_id = "proj-1"
    watcher._mode = "local"

    with (
        patch.object(watcher, "_run_checks", return_value=True),
        patch.object(watcher, "_attempt_pr", return_value="success"),
        patch.object(watcher, "_safe_set_state"),
        patch.object(watcher, "_preserve_worker_log"),
        patch.object(watcher, "_cleanup_worktree"),
    ):
        watcher._finalize_worker(worker, returncode=0, wall_time=42.0)

    assert len(recorded) == 1
    m = recorded[0]
    assert m.local_tokens == 2400
    assert m.context_compactions == 5
