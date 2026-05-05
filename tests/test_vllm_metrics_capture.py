"""Tests for the WOR-370 vLLM /metrics capture helpers + integrations.

Three layers:

1. ``capture_vllm_metrics`` — Prometheus text parsing + HTTP fetch handling.
2. ``compute_vllm_metrics_delta`` — counter math + derived ratios.
3. Dispatch + finalize integration — solo-tracking flag flow and the
   sentinel artifact path under concurrent dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.watcher.watcher_helpers import (
    capture_vllm_metrics,
    compute_vllm_metrics_delta,
)

# ---------------------------------------------------------------------------
# capture_vllm_metrics — HTTP + Prometheus text parsing
# ---------------------------------------------------------------------------


_SAMPLE_METRICS = b"""\
# HELP vllm:prefix_cache_hits_total ...
# TYPE vllm:prefix_cache_hits_total counter
vllm:prefix_cache_hits_total{model="qwen3-coder",engine="0"} 1454624.0
vllm:prefix_cache_queries_total{model="qwen3-coder",engine="0"} 1546626.0
vllm:prompt_tokens_total{model="qwen3-coder",engine="0"} 1546626.0
vllm:generation_tokens_total{model="qwen3-coder",engine="0"} 6839.0
vllm:time_to_first_token_seconds_sum{model="qwen3-coder",engine="0"} 42.5
vllm:time_to_first_token_seconds_count{model="qwen3-coder",engine="0"} 28.0
vllm:num_preemptions_total{model="qwen3-coder",engine="0"} 0.0
# An unrelated metric we should ignore
some_other_metric{foo="bar"} 999.0
"""


def _conn_with_response(status: int, body: bytes) -> MagicMock:
    """Build a mock that emulates http.client.HTTPConnection."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    conn = MagicMock()
    conn.getresponse.return_value = resp
    return conn


def test_capture_returns_aggregated_dict() -> None:
    conn = _conn_with_response(200, _SAMPLE_METRICS)
    with patch("http.client.HTTPConnection", return_value=conn):
        result = capture_vllm_metrics()
    assert result is not None
    assert result["vllm:prefix_cache_hits_total"] == 1454624.0
    assert result["vllm:prefix_cache_queries_total"] == 1546626.0
    assert result["vllm:num_preemptions_total"] == 0.0


def test_capture_returns_none_on_oserror() -> None:
    conn = MagicMock()
    conn.request.side_effect = OSError("network down")
    with patch("http.client.HTTPConnection", return_value=conn):
        result = capture_vllm_metrics()
    assert result is None


def test_capture_returns_none_on_http_exception() -> None:
    import http.client as http_client

    conn = MagicMock()
    conn.request.side_effect = http_client.HTTPException("bad response")
    with patch("http.client.HTTPConnection", return_value=conn):
        result = capture_vllm_metrics()
    assert result is None


def test_capture_returns_none_on_non_200_status() -> None:
    """A 4xx/5xx response means the endpoint isn't healthy — treat as failure."""
    conn = _conn_with_response(503, b"")
    with patch("http.client.HTTPConnection", return_value=conn):
        result = capture_vllm_metrics()
    assert result is None


def test_capture_returns_none_when_no_target_metrics_present() -> None:
    """An endpoint that responds 200 with unrelated content is treated as failure."""
    body = b'# random metrics\nsome_unrelated{foo="bar"} 1.0\n'
    conn = _conn_with_response(200, body)
    with patch("http.client.HTTPConnection", return_value=conn):
        result = capture_vllm_metrics()
    assert result is None


def test_capture_aggregates_across_label_combinations() -> None:
    """Two label sets for the same metric should sum (defensive)."""
    body = (
        b'vllm:prompt_tokens_total{engine="0"} 100.0\n'
        b'vllm:prompt_tokens_total{engine="1"} 50.0\n'
    )
    conn = _conn_with_response(200, body)
    with patch("http.client.HTTPConnection", return_value=conn):
        result = capture_vllm_metrics()
    assert result is not None
    assert result["vllm:prompt_tokens_total"] == 150.0


# ---------------------------------------------------------------------------
# compute_vllm_metrics_delta
# ---------------------------------------------------------------------------


def test_delta_basic_arithmetic() -> None:
    before = {
        "vllm:prefix_cache_hits_total": 100.0,
        "vllm:prefix_cache_queries_total": 200.0,
        "vllm:prompt_tokens_total": 1000.0,
        "vllm:generation_tokens_total": 50.0,
        "vllm:time_to_first_token_seconds_sum": 5.0,
        "vllm:time_to_first_token_seconds_count": 10.0,
        "vllm:num_preemptions_total": 0.0,
    }
    after = {
        "vllm:prefix_cache_hits_total": 196.0,
        "vllm:prefix_cache_queries_total": 300.0,
        "vllm:prompt_tokens_total": 1500.0,
        "vllm:generation_tokens_total": 70.0,
        "vllm:time_to_first_token_seconds_sum": 11.0,
        "vllm:time_to_first_token_seconds_count": 14.0,
        "vllm:num_preemptions_total": 1.0,
    }
    d = compute_vllm_metrics_delta(before, after)
    assert d["prefix_cache_hits"] == 96
    assert d["prefix_cache_queries"] == 100
    assert d["prefix_cache_hit_ratio"] == pytest.approx(0.96)
    assert d["prompt_tokens"] == 500
    assert d["generation_tokens"] == 20
    assert d["ttft_seconds_sum"] == pytest.approx(6.0)
    assert d["ttft_count"] == 4
    assert d["ttft_mean_seconds"] == pytest.approx(1.5)
    assert d["preemptions"] == 1


def test_delta_zero_queries_yields_none_hit_ratio() -> None:
    before = {
        "vllm:prefix_cache_hits_total": 0.0,
        "vllm:prefix_cache_queries_total": 0.0,
        "vllm:time_to_first_token_seconds_count": 0.0,
        "vllm:time_to_first_token_seconds_sum": 0.0,
    }
    after = dict(before)
    d = compute_vllm_metrics_delta(before, after)
    assert d["prefix_cache_hit_ratio"] is None
    assert d["ttft_mean_seconds"] is None


def test_delta_negative_counter_treated_as_corrupt() -> None:
    """vLLM restart between snapshots resets counters; treat result as None."""
    before = {"vllm:prefix_cache_hits_total": 1000.0}
    after = {"vllm:prefix_cache_hits_total": 50.0}  # restart happened
    d = compute_vllm_metrics_delta(before, after)
    assert d["prefix_cache_hits"] is None
    assert d["prefix_cache_hit_ratio"] is None


# ---------------------------------------------------------------------------
# Integration: dispatch.start_ticket
# ---------------------------------------------------------------------------


def _services(mode: str = "default", vllm_healthy: bool = True) -> MagicMock:
    services = MagicMock()
    services._mode = mode
    services.probe_vllm_health.return_value = vllm_healthy
    return services


def test_dispatch_captures_vllm_snapshot_when_solo(tmp_path: Path) -> None:
    """First worker: dispatch_concurrency=0, snapshot taken, remained_solo=True."""
    from app.core.watcher.dispatch import start_ticket
    from tests.conftest import make_manifest

    manifest = make_manifest(
        implementation_mode="local",
        base_branch="main",
    )
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    sample_snapshot = {"vllm:prompt_tokens_total": 100.0}

    with (
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "worktree",
        ),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch(
            "app.core.watcher.dispatch.capture_vllm_metrics",
            return_value=sample_snapshot,
        ) as mock_capture,
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
            _dedup_state={},
        )

    assert len(local_active) == 1
    worker = local_active[0]
    assert worker.dispatch_concurrency == 0
    assert worker.vllm_metrics_before == sample_snapshot
    assert worker.remained_solo is True
    mock_capture.assert_called_once()


def test_dispatch_invalidates_solo_peer_when_second_worker_launches(
    tmp_path: Path,
) -> None:
    """When a second worker dispatches, any existing solo worker loses its flag."""
    from app.core.watcher.dispatch import start_ticket
    from app.core.watcher.watcher_types import ActiveWorker
    from tests.conftest import make_manifest

    # Pre-existing solo worker in the pool
    solo_manifest = make_manifest(ticket_id="WOR-1")
    solo_worker = ActiveWorker(
        ticket_id="WOR-1",
        linear_id="linear-1",
        manifest=solo_manifest,
        worktree_path=tmp_path / "wt-1",
        process=MagicMock(),
        vllm_metrics_before={"vllm:prompt_tokens_total": 100.0},
        remained_solo=True,
    )
    local_active: list = [solo_worker]
    cloud_active: list = []

    new_manifest = make_manifest(
        ticket_id="WOR-2", base_branch="main", worker_branch="wor-2"
    )
    linear = MagicMock()
    services = _services()

    with (
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "wt-2",
        ),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
        # Should NOT be called: dispatch_concurrency > 0 means no snapshot
        patch(
            "app.core.watcher.dispatch.capture_vllm_metrics",
            return_value={"vllm:prompt_tokens_total": 200.0},
        ) as mock_capture,
    ):
        start_ticket(
            manifest=new_manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-2",
            ticket_id="WOR-2",
            _escalation_policy=MagicMock(),
            _dedup_state={},
        )

    # Pre-existing worker has lost its solo flag
    assert solo_worker.remained_solo is False
    # The new worker did NOT take its own snapshot (concurrency > 0)
    new_worker = local_active[-1]
    assert new_worker.dispatch_concurrency == 1
    assert new_worker.remained_solo is False
    assert new_worker.vllm_metrics_before is None
    mock_capture.assert_not_called()


def test_dispatch_skips_snapshot_when_metrics_endpoint_unreachable(
    tmp_path: Path,
) -> None:
    """If /metrics returns None (unreachable), worker proceeds without solo flag."""
    from app.core.watcher.dispatch import start_ticket
    from tests.conftest import make_manifest

    manifest = make_manifest(implementation_mode="local", base_branch="main")
    linear = MagicMock()
    services = _services()
    local_active: list = []
    cloud_active: list = []

    with (
        patch(
            "app.core.watcher.dispatch.create_worktree",
            return_value=tmp_path / "worktree",
        ),
        patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
        patch("app.core.watcher.dispatch.write_worker_pytest_config"),
        patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
        patch("app.core.watcher.dispatch.launch_worker", return_value=MagicMock()),
        patch("app.core.watcher.dispatch.safe_set_state"),
        patch(
            "app.core.watcher.dispatch.capture_vllm_metrics",
            return_value=None,  # endpoint unreachable
        ),
    ):
        start_ticket(
            manifest=manifest,
            linear=linear,
            services=services,
            worker_verbose=False,
            _local_active=local_active,
            _cloud_active=cloud_active,
            max_cloud_workers=3,
            _repo_root=tmp_path,
            _processed_tickets=[],
            linear_id="fake-linear-id",
            ticket_id="WOR-10",
            _escalation_policy=MagicMock(),
            _dedup_state={},
        )

    worker = local_active[0]
    assert worker.dispatch_concurrency == 0
    assert worker.vllm_metrics_before is None
    assert worker.remained_solo is False


# ---------------------------------------------------------------------------
# Integration: finalize_worker — sentinel artifact paths
# ---------------------------------------------------------------------------


def test_finalize_writes_attributable_artifact_for_solo_worker(tmp_path: Path) -> None:
    """A solo worker with valid before/after produces the full deltas artifact."""
    from app.core.watcher.watcher_finalize import _write_vllm_metrics_artifact

    _write_vllm_metrics_artifact(
        tmp_path,
        "WOR-50",
        attributable=True,
        before={"vllm:prefix_cache_hits_total": 100.0},
        after={"vllm:prefix_cache_hits_total": 150.0},
        deltas={"prefix_cache_hits": 50, "prefix_cache_hit_ratio": 0.85},
    )

    artifact = tmp_path / ".claude" / "artifacts" / "wor_50" / "vllm_metrics.json"
    assert artifact.exists()
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["ticket_id"] == "WOR-50"
    assert payload["attributable"] is True
    assert payload["before"]["vllm:prefix_cache_hits_total"] == 100.0
    assert payload["after"]["vllm:prefix_cache_hits_total"] == 150.0
    assert payload["deltas"]["prefix_cache_hits"] == 50


def test_finalize_writes_sentinel_artifact_for_concurrent_session(
    tmp_path: Path,
) -> None:
    """Non-solo session writes a sentinel with reason; no after/deltas."""
    from app.core.watcher.watcher_finalize import _write_vllm_metrics_artifact

    _write_vllm_metrics_artifact(
        tmp_path,
        "WOR-51",
        attributable=False,
        before={"vllm:prompt_tokens_total": 1000.0},
        after=None,
        deltas={},
        reason="concurrent worker dispatched during session",
    )

    artifact = tmp_path / ".claude" / "artifacts" / "wor_51" / "vllm_metrics.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["attributable"] is False
    assert "concurrent" in payload["reason"]
    assert "before" in payload  # before is still useful for audit
    assert "after" not in payload
    assert "deltas" not in payload
