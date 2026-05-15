"""Tests for multi-dispatch-per-cycle and inter-dispatch delay (WOR-419).

Tests the _dispatch_next_ticket loop change: instead of returning after one
successful dispatch, the watcher iterates eligible tickets and dispatches up
to MAX_DISPATCHES_PER_CYCLE per poll cycle, with DISPATCH_DELAY_SECONDS sleep
between successive dispatches.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.manifest import ArtifactPaths, ExecutionManifest
from app.core.watcher.watcher import Watcher


def _make_manifest(**overrides: Any) -> ExecutionManifest:
    defaults: dict[str, Any] = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test ticket",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "routing": "local",
        "review_mode": "auto",
        "base_branch": "wor-96-local-worker-engine",
        "worker_branch": "wor-10-test-ticket",
        "objective": "Do the thing.",
        "artifact_paths": ArtifactPaths.from_ticket_id("WOR-10"),
        "allowed_paths": ["app/core/foo.py"],
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)


def _regular_ticket(identifier: str = "WOR-99") -> dict[str, Any]:
    return {
        "id": "fake-linear-id",
        "identifier": identifier,
        "title": "Regular ticket",
        "labels": {"nodes": [{"name": "local-ready"}]},
    }


class TestMultiDispatchPerCycle:
    """_dispatch_next_ticket dispatches multiple eligible tickets per cycle."""

    def test_dispatches_multiple_eligible_tickets_in_one_cycle(
        self, tmp_path: Path, caplog: pytest.LogCaptureContext
    ) -> None:
        """Given 4 eligible tickets and an empty pool, watcher dispatches all 4
        in one cycle instead of just 1."""
        manifest1 = _make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
            allowed_paths=["app/core/a.py"],
        )
        manifest2 = _make_manifest(
            ticket_id="WOR-11",
            worker_branch="wor-11-test-ticket",
            allowed_paths=["app/core/b.py"],
        )
        manifest3 = _make_manifest(
            ticket_id="WOR-12",
            worker_branch="wor-12-test-ticket",
            allowed_paths=["app/core/c.py"],
        )
        manifest4 = _make_manifest(
            ticket_id="WOR-13",
            worker_branch="wor-13-test-ticket",
            allowed_paths=["app/core/d.py"],
        )

        linear_mock = MagicMock()
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [
            _regular_ticket("WOR-10"),
            _regular_ticket("WOR-11"),
            _regular_ticket("WOR-12"),
            _regular_ticket("WOR-13"),
        ]

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )

        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str):
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            manifest_map = {
                "WOR-10": manifest1,
                "WOR-11": manifest2,
                "WOR-12": manifest3,
                "WOR-13": manifest4,
            }
            return manifest_map[ticket_id]

        fake_process = MagicMock(spec=subprocess.Popen)

        with (
            patch.object(w, "_load_manifest", side_effect=capture_load),
            patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
            patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
            patch("app.core.watcher.dispatch.write_worker_pytest_config"),
            patch("app.core.watcher.dispatch.safe_set_state"),
            patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
            patch(
                "app.core.watcher.dispatch.launch_worker",
                return_value=fake_process,
            ),
            patch.object(w._services, "ensure_vllm_anthropic_mode"),
            patch.object(w._services, "probe_vllm_health", return_value=True),
            caplog.at_level(logging.INFO, logger="app.core.watcher"),
        ):
            w._dispatch_next_ticket()

        # All 4 tickets should have been dispatch_count
        for tid in ("WOR-10", "WOR-11", "WOR-12", "WOR-13"):
            assert load_counts.get(tid, 0) == 1, f"{tid} was not dispatch_count"

        assert len(w._local_active) == 4
        for tid in ("WOR-10", "WOR-11", "WOR-12", "WOR-13"):
            assert any(w._local_active[i].ticket_id == tid for i in range(4)), (
                f"{tid} not in local_active"
            )

    def test_respects_max_dispatches_per_cycle(self, tmp_path: Path) -> None:
        """Given 8 eligible tickets but MAX_DISPATCHES_PER_CYCLE=4,
        only 4 are dispatch_count."""
        from app.core.watcher.watcher import MAX_DISPATCHES_PER_CYCLE

        manifests = [
            _make_manifest(
                ticket_id=f"WOR-{i}",
                worker_branch=f"wor-{i}-test-ticket",
                allowed_paths=[f"app/core/{chr(97 + i)}.py"],
            )
            for i in range(8)
        ]

        linear_mock = MagicMock()
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [
            _regular_ticket(f"WOR-{i}") for i in range(8)
        ]

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )

        load_counts: dict[str, int] = {}

        def load_for(ticket_id: str):
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifests[int(ticket_id[4:])]

        fake_process = MagicMock(spec=subprocess.Popen)

        with (
            patch.object(w, "_load_manifest", side_effect=load_for),
            patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
            patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
            patch("app.core.watcher.dispatch.write_worker_pytest_config"),
            patch("app.core.watcher.dispatch.safe_set_state"),
            patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
            patch(
                "app.core.watcher.dispatch.launch_worker",
                return_value=fake_process,
            ),
            patch.object(w._services, "ensure_vllm_anthropic_mode"),
            patch.object(w._services, "probe_vllm_health", return_value=True),
        ):
            w._dispatch_next_ticket()

        dispatch_count = {k: v for k, v in load_counts.items() if v > 0}
        assert len(dispatch_count) == MAX_DISPATCHES_PER_CYCLE

    def test_skips_spiked_tickets_does_not_count_toward_limit(
        self, tmp_path: Path
    ) -> None:
        """Spike-labelled tickets are skipped (continue) and do not count
        toward the MAX_DISPATCHES_PER_CYCLE limit — eligible non-spike tickets
        still get dispatch_count."""

        spike_ticket: dict[str, Any] = {
            "id": "fake-linear-id",
            "identifier": "WOR-SPIKE",
            "title": "Spike ticket",
            "labels": {"nodes": [{"name": "Spike"}]},
        }

        manifests = [
            _make_manifest(
                ticket_id=f"WOR-{i}",
                worker_branch=f"wor-{i}-test-ticket",
                allowed_paths=[f"app/core/{chr(97 + i)}.py"],
            )
            for i in range(3)
        ]

        linear_mock = MagicMock()
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [
            spike_ticket,  # spike — should be skipped
            _regular_ticket("WOR-10"),
            _regular_ticket("WOR-11"),
            _regular_ticket("WOR-12"),
        ]

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )

        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str):
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            idx = int(ticket_id[4:]) - 10
            if 0 <= idx < len(manifests):
                return manifests[idx]
            return manifests[0]

        fake_process = MagicMock(spec=subprocess.Popen)

        with (
            patch.object(w, "_load_manifest", side_effect=capture_load),
            patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
            patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
            patch("app.core.watcher.dispatch.write_worker_pytest_config"),
            patch("app.core.watcher.dispatch.safe_set_state"),
            patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
            patch(
                "app.core.watcher.dispatch.launch_worker",
                return_value=fake_process,
            ),
            patch.object(w._services, "ensure_vllm_anthropic_mode"),
            patch.object(w._services, "probe_vllm_health", return_value=True),
        ):
            w._dispatch_next_ticket()

        # All 3 eligible tickets should be dispatch_count (spike skipped, doesn't
        # consume the dispatch limit)
        for tid in ("WOR-10", "WOR-11", "WOR-12"):
            assert load_counts.get(tid, 0) == 1, f"{tid} was not dispatch_count"
        assert load_counts.get("WOR-SPIKE", 0) == 0


class TestInterDispatchDelay:
    """Inter-dispatch delay constants and behavior."""

    def test_delay_constant_is_module_level(self) -> None:
        """DISPATCH_DELAY_SECONDS is defined as a module-level constant."""
        from app.core.watcher.watcher import DISPATCH_DELAY_SECONDS

        assert isinstance(DISPATCH_DELAY_SECONDS, float)
        assert DISPATCH_DELAY_SECONDS == 2.5

    def test_max_dispatches_constant_is_module_level(self) -> None:
        """MAX_DISPATCHES_PER_CYCLE is defined as a module-level constant."""
        from app.core.watcher.watcher import MAX_DISPATCHES_PER_CYCLE

        assert isinstance(MAX_DISPATCHES_PER_CYCLE, int)
        assert MAX_DISPATCHES_PER_CYCLE == 4

    def test_delay_is_called_between_dispatches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """time.sleep is called between each dispatch to avoid Linear API spike."""
        sleep_calls: list[float] = []

        def record_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        manifest = _make_manifest(
            ticket_id="WOR-10",
            worker_branch="wor-10-test-ticket",
            allowed_paths=["app/core/a.py"],
        )
        manifest2 = _make_manifest(
            ticket_id="WOR-11",
            worker_branch="wor-11-test-ticket",
            allowed_paths=["app/core/b.py"],
        )

        linear_mock = MagicMock()
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [
            _regular_ticket("WOR-10"),
            _regular_ticket("WOR-11"),
        ]

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )

        load_index = [0]

        def capture_load(ticket_id: str):
            idx = load_index[0]
            load_index[0] += 1
            return manifest if idx == 0 else manifest2

        fake_process = MagicMock(spec=subprocess.Popen)

        with (
            patch.object(w, "_load_manifest", side_effect=capture_load),
            patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
            patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
            patch("app.core.watcher.dispatch.write_worker_pytest_config"),
            patch("app.core.watcher.dispatch.safe_set_state"),
            patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
            patch(
                "app.core.watcher.dispatch.launch_worker",
                return_value=fake_process,
            ),
            patch.object(w._services, "ensure_vllm_anthropic_mode"),
            patch.object(w._services, "probe_vllm_health", return_value=True),
            patch("app.core.watcher.watcher.time.sleep", record_sleep),
            patch("app.core.watcher.watcher.MAX_DISPATCHES_PER_CYCLE", 2),
        ):
            w._dispatch_next_ticket()

        # Exactly one sleep call between two dispatches; break after 2 hits
        # so no sleep after the final dispatch.
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 2.5


class TestFailureDoesNotConsumeLimit:
    """A failed _start_ticket (exception) does not consume the dispatch limit."""

    def test_exception_does_not_break_loop(self, tmp_path: Path) -> None:
        """When _start_ticket raises, the loop continues to the next ticket."""

        manifests = [
            _make_manifest(
                ticket_id="WOR-FAIL",
                worker_branch="wor-fail-ticket",
                allowed_paths=["app/core/evil.py"],
            ),
            _make_manifest(
                ticket_id="WOR-10",
                worker_branch="wor-10-test-ticket",
                allowed_paths=["app/core/a.py"],
            ),
        ]

        linear_mock = MagicMock()
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [
            {"identifier": "WOR-FAIL", "id": "fake", "labels": {"nodes": []}},
            _regular_ticket("WOR-10"),
        ]

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )

        with (
            patch.object(w, "_load_manifest") as mock_load,
            patch("app.core.watcher.dispatch.create_worktree"),
        ):
            mock_load.side_effect = [
                RuntimeError("boom"),  # First ticket fails
                manifests[1],  # Second ticket succeeds
            ]
            # Patch safe_set_state to avoid actual Linear call
            with patch("app.core.watcher.dispatch.safe_set_state"):
                w._dispatch_next_ticket()

        # Second ticket should have been loaded despite first failure
        calls = mock_load.call_args_list
        assert len(calls) == 2
        assert calls[0][0][0] == "WOR-FAIL"
        assert calls[1][0][0] == "WOR-10"


class TestMultiDispatchTimingE2E:
    """End-to-end timing: 4 tickets → 3 inter-dispatch sleep(2.5) calls."""

    def test_4_dispatches_with_timing_assertion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given 4 eligible tickets and an empty pool, watcher dispatches all
        4 in one cycle with exactly 3 inter-dispatch gaps of 2.5s."""
        sleep_times: list[float] = []

        def record_sleep(seconds: float) -> None:
            sleep_times.append(seconds)

        manifests = [
            _make_manifest(
                ticket_id=f"WOR-{i}",
                worker_branch=f"wor-{i}-test-ticket",
                allowed_paths=[f"app/core/{chr(97 + i)}.py"],
            )
            for i in range(4)
        ]

        linear_mock = MagicMock()
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [
            _regular_ticket(f"WOR-{i}") for i in range(4)
        ]

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )

        load_index = [0]

        def capture_load(ticket_id: str):
            idx = load_index[0]
            load_index[0] += 1
            return manifests[idx]

        fake_process = MagicMock(spec=subprocess.Popen)

        with (
            patch.object(w, "_load_manifest", side_effect=capture_load),
            patch("app.core.watcher.dispatch.create_worktree", return_value=tmp_path),
            patch("app.core.watcher.dispatch.copy_manifest_to_worktree"),
            patch("app.core.watcher.dispatch.write_worker_pytest_config"),
            patch("app.core.watcher.dispatch.safe_set_state"),
            patch("app.core.watcher.dispatch.backup_plan_files", return_value=[]),
            patch(
                "app.core.watcher.dispatch.launch_worker",
                return_value=fake_process,
            ),
            patch.object(w._services, "ensure_vllm_anthropic_mode"),
            patch.object(w._services, "probe_vllm_health", return_value=True),
            patch("app.core.watcher.watcher.time.sleep", record_sleep),
        ):
            w._dispatch_next_ticket()

        # 4 dispatches, 3 sleep calls between them
        assert len(sleep_times) == 3
        for t in sleep_times:
            assert t == 2.5

        # All 4 tickets should have been dispatch_count
        for tid in ("WOR-0", "WOR-1", "WOR-2", "WOR-3"):
            assert any(w._local_active[i].ticket_id == tid for i in range(4)), (
                f"{tid} not in local_active"
            )
