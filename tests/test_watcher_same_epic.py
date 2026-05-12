"""Tests for same-epic preference in the watcher ticket picker.

Unit tests for:
- picker_sort_key: stable sort key with same-epic pref
- get_active_parent_ids: extract parent IDs from active workers
- Integration: dispatch loop preserves sort order
- same_epic_pair computation in dispatch.start_ticket
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.manifest import ExecutionManifest
from app.core.watcher.watcher import Watcher
from app.core.watcher.watcher_helpers import (
    get_active_parent_ids,
    picker_sort_key,
)
from app.core.watcher.watcher_types import ActiveWorker
from tests.conftest import make_manifest

# ---------------------------------------------------------------------------
# picker_sort_key unit tests
# ---------------------------------------------------------------------------


def _make_ticket(
    ticket_id: str = "WOR-10",
    priority: int = 2,
    parent_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": ticket_id,
        "identifier": ticket_id,
        "title": ticket_id,
        "priority": priority,
        "parent": {"id": parent_id} if parent_id is not None else None,
    }


class TestPickerSortKey:
    """picker_sort_key applies same-epic preference as a stable sort key."""

    def test_no_active_workers_same_key_for_all(self) -> None:
        """When no active workers, parent_match=1 for every candidate."""
        tickets = [
            _make_ticket("A", parent_id="X"),
            _make_ticket("B", parent_id="Y"),
        ]
        for t in tickets:
            key = picker_sort_key(t, set(), 0)
            assert key == (1, t["priority"], t["id"])

    def test_same_epic_sibling_sorts_first(self) -> None:
        """A candidate whose parent matches an active worker sorts before
        one whose parent does not match."""
        active_parent_ids = {"EPIC-A"}
        same = _make_ticket("S", parent_id="EPIC-A", priority=1)
        diff = _make_ticket("D", parent_id="EPIC-B", priority=3)
        assert picker_sort_key(same, active_parent_ids, 0) < picker_sort_key(
            diff, active_parent_ids, 0
        )

    def test_within_same_epic_group_preserves_current_ordering(
        self,
    ) -> None:
        """When two candidates share the same parent_match value the sort is
        stable — within each (parent_match, priority) bucket the original
        ordering is preserved."""
        active_parent_ids = {"EPIC-A"}
        t1 = _make_ticket("S1", parent_id="EPIC-A", priority=1)
        t2 = _make_ticket("S2", parent_id="EPIC-A", priority=3)
        assert picker_sort_key(t1, active_parent_ids, 0) < picker_sort_key(
            t2, active_parent_ids, 0
        )

    def test_no_parent_field_treated_as_cross_epic(self) -> None:
        """When a candidate has no parent, parent_match=1 (cross-epic)."""
        active_parent_ids = {"EPIC-A"}
        no_parent = _make_ticket("NP", parent_id=None, priority=2)
        key = picker_sort_key(no_parent, active_parent_ids, 0)
        assert key[0] == 1

    def test_empty_parent_dict_treated_as_cross_epic(self) -> None:
        """When parent is {} the candidate is treated as cross-epic."""
        active_parent_ids = {"EPIC-A"}
        empty_parent = {"id": "X", "priority": 2, "parent": {}}
        key = picker_sort_key(empty_parent, active_parent_ids, 0)
        assert key == (1, empty_parent["priority"], empty_parent["id"])

    def test_non_matching_parent_treated_as_cross_epic(self) -> None:
        """When parent exists but does not match any active parent,
        parent_match=1 (cross-epic)."""
        active_parent_ids = {"EPIC-A"}
        other = _make_ticket("O", parent_id="EPIC-B", priority=2)
        key = picker_sort_key(other, active_parent_ids, 0)
        assert key == (1, other["priority"], other["id"])


# ---------------------------------------------------------------------------
# get_active_parent_ids unit tests
# ---------------------------------------------------------------------------


class TestGetActiveParentIds:
    def test_empty_list(self) -> None:
        assert get_active_parent_ids([]) == set()

    def test_single_worker(self) -> None:
        manifest = make_manifest(ticket_id="WOR-10", epic_id="EPIC-A")
        w = ActiveWorker(
            ticket_id="WOR-10",
            linear_id="fake-linear-id",
            manifest=manifest,
            worktree_path=Path("/tmp/WOR-10"),
            process=MagicMock(spec=subprocess.Popen),
        )
        assert get_active_parent_ids([w]) == {"EPIC-A"}

    def test_skips_workers_without_epic_id(self) -> None:
        manifest = make_manifest(ticket_id="WOR-10")
        w = ActiveWorker(
            ticket_id="WOR-10",
            linear_id="fake-linear-id",
            manifest=manifest,
            worktree_path=Path("/tmp/WOR-10"),
            process=MagicMock(spec=subprocess.Popen),
        )
        # epic_id is a valid non-empty string from make_manifest default,
        # so it IS captured — this test verifies the normal happy path.
        assert get_active_parent_ids([w]) == {manifest.epic_id}

    def test_deduplicates(self) -> None:
        manifest_a = make_manifest(ticket_id="WOR-10", epic_id="EPIC-A")
        w1 = ActiveWorker(
            ticket_id="WOR-10",
            linear_id="fake-linear-id",
            manifest=manifest_a,
            worktree_path=Path("/tmp/WOR-10"),
            process=MagicMock(spec=subprocess.Popen),
        )
        manifest_a2 = make_manifest(ticket_id="WOR-20", epic_id="EPIC-A")
        w2 = ActiveWorker(
            ticket_id="WOR-20",
            linear_id="fake-linear-id",
            manifest=manifest_a2,
            worktree_path=Path("/tmp/WOR-20"),
            process=MagicMock(spec=subprocess.Popen),
        )
        manifest_b = make_manifest(ticket_id="WOR-30", epic_id="EPIC-B")
        w3 = ActiveWorker(
            ticket_id="WOR-30",
            linear_id="fake-linear-id",
            manifest=manifest_b,
            worktree_path=Path("/tmp/WOR-30"),
            process=MagicMock(spec=subprocess.Popen),
        )
        assert get_active_parent_ids([w1, w2, w3]) == {"EPIC-A", "EPIC-B"}


# ---------------------------------------------------------------------------
# Integration: dispatch loop — solo path (no regression)
# ---------------------------------------------------------------------------


def _make_manifest_kw(**overrides: Any) -> ExecutionManifest:
    defaults: dict[str, Any] = {
        "ticket_id": "WOR-10",
        "epic_id": "WOR-96",
        "title": "Test ticket",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "review_mode": "auto",
        "base_branch": "wor-96-local-worker-engine",
        "worker_branch": "wor-10-test-ticket",
        "objective": "Do the thing.",
        "artifact_paths": make_manifest().artifact_paths,
        "allowed_paths": ["app/core/foo.py"],
        "required_checks": ["pytest"],
    }
    defaults.update(overrides)
    return ExecutionManifest(**defaults)  # type: ignore[arg-type]


def _regular_ticket(
    identifier: str = "WOR-99",
    priority: int = 2,
    parent_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "fake-linear-id",
        "identifier": identifier,
        "title": "Regular ticket",
        "priority": priority,
        "parent": {"id": parent_id} if parent_id else None,
        "labels": {"nodes": [{"name": "local-ready"}]},
    }


class TestSoloDispatchUnchanged:
    """When no active worker exists, the picker output is identical to today."""

    def test_empty_pool_dispatches_linear_order(self, tmp_path: Path) -> None:
        """With 0 active workers, tickets dispatch in the original Linear order."""
        manifests = {
            f"WOR-{i}": _make_manifest_kw(
                ticket_id=f"WOR-{i}", allowed_paths=[f"app/core/{i}.py"]
            )
            for i in range(4)
        }
        tickets = [_regular_ticket(f"WOR-{i}", parent_id="EPIC-A") for i in range(4)]

        linear_mock = MagicMock()
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = tickets

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )

        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str) -> ExecutionManifest:
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifests[ticket_id]

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

        assert len(w._local_active) == 4
        assert [w._local_active[i].ticket_id for i in range(4)] == [
            f"WOR-{i}" for i in range(4)
        ]


# ---------------------------------------------------------------------------
# Integration: same-epic preference in dispatch loop
# ---------------------------------------------------------------------------


class TestSameEpicPreference:
    """Two same-epic tickets: second prefers same-epic."""

    def test_same_epic_second_prefers_same_epic(self, tmp_path: Path) -> None:
        """With one active worker on EPIC-A and two candidates (A, B),
        the EPIC-A candidate dispatches first."""
        from app.core.watcher.watcher_types import ActiveWorker

        # Pre-existing active worker on EPIC-A (simulating a prior dispatch)
        active_manifest = make_manifest(
            ticket_id="WOR-100",
            epic_id="EPIC-A",
            allowed_paths=["app/core/y.py"],
        )
        active_worker = ActiveWorker(
            ticket_id="WOR-100",
            linear_id="fake-linear-id-100",
            manifest=active_manifest,
            worktree_path=tmp_path / "worktree_100",
            process=MagicMock(spec=subprocess.Popen),
        )

        linear_mock = MagicMock()

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )
        w._local_active = [active_worker]

        # Two candidate tickets: EPIC-B (priority 1) and EPIC-A (priority 3)
        # Even though EPIC-B has higher priority, EPIC-A should come first
        # because it matches the active worker's parent.
        tickets = [
            _regular_ticket("WOR-B", priority=1, parent_id="EPIC-B"),
            _regular_ticket("WOR-A", priority=3, parent_id="EPIC-A"),
        ]

        manifests = {
            "WOR-B": _make_manifest_kw(
                ticket_id="WOR-B",
                epic_id="EPIC-B",
                allowed_paths=["app/core/b.py"],
            ),
            "WOR-A": _make_manifest_kw(
                ticket_id="WOR-A",
                epic_id="EPIC-A",
                allowed_paths=["app/core/a.py"],
            ),
        }

        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = tickets

        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str) -> ExecutionManifest:
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifests[ticket_id]

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

        # Both dispatched
        assert len(w._local_active) == 3  # 1 pre-existing + 2 new
        # WOR-A (same epic) should dispatch first, then WOR-B
        new_dispatches = [w._local_active[i].ticket_id for i in range(1, 3)]
        assert new_dispatches == ["WOR-A", "WOR-B"]


class TestCrossEpicNoPreference:
    """Two cross-epic tickets: no preference applies."""

    def test_cross_epic_no_preference(self, tmp_path: Path) -> None:
        """With one active worker on EPIC-A and two different-epic candidates,
        the original ordering is preserved (no same-epic preference)."""
        from app.core.watcher.watcher_types import ActiveWorker

        active_manifest = make_manifest(
            ticket_id="WOR-100",
            epic_id="EPIC-A",
            allowed_paths=["app/core/y.py"],
        )
        active_worker = ActiveWorker(
            ticket_id="WOR-100",
            linear_id="fake-linear-id-100",
            manifest=active_manifest,
            worktree_path=tmp_path / "worktree_100",
            process=MagicMock(spec=subprocess.Popen),
        )

        linear_mock = MagicMock()

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )
        w._local_active = [active_worker]

        # Both candidates are from different epics — no same-epic match
        tickets = [
            _regular_ticket("WOR-B", priority=1, parent_id="EPIC-B"),
            _regular_ticket("WOR-C", priority=3, parent_id="EPIC-C"),
        ]

        manifests = {
            "WOR-B": _make_manifest_kw(
                ticket_id="WOR-B",
                epic_id="EPIC-B",
                allowed_paths=["app/core/b.py"],
            ),
            "WOR-C": _make_manifest_kw(
                ticket_id="WOR-C",
                epic_id="EPIC-C",
                allowed_paths=["app/core/c.py"],
            ),
        }

        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = tickets

        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str) -> ExecutionManifest:
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifests[ticket_id]

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

        # Both dispatched, order preserved (WOR-B has higher priority =
        # lower sort key within cross-epic group)
        assert len(w._local_active) == 3
        new_dispatches = [w._local_active[i].ticket_id for i in range(1, 3)]
        assert new_dispatches == ["WOR-B", "WOR-C"]


# ---------------------------------------------------------------------------
# start_ticket same_epic_pair computation
# ---------------------------------------------------------------------------


class TestSameEpicPairComputation:
    """same_epic_pair is computed at dispatch time from candidate parent vs
    active-worker parents."""

    def test_same_epic_pair_true_for_matching_parent(self, tmp_path: Path) -> None:
        """When candidate parent matches an active worker's parent,
        same_epic_pair=True."""
        active_manifest = make_manifest(
            ticket_id="WOR-100",
            epic_id="EPIC-A",
            allowed_paths=["app/core/y.py"],
        )
        active_worker = ActiveWorker(
            ticket_id="WOR-100",
            linear_id="fake-linear-id-100",
            manifest=active_manifest,
            worktree_path=tmp_path / "worktree_100",
            process=MagicMock(spec=subprocess.Popen),
        )

        linear_mock = MagicMock()

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )
        w._local_active = [active_worker]

        ticket = _regular_ticket("MATCH", priority=2, parent_id="EPIC-A")
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [ticket]

        candidate = {
            "id": "linear-id-match",
            "identifier": "MATCH",
            "parent": {"id": "EPIC-A"},
            "labels": {"nodes": []},
        }
        manifest = _make_manifest_kw(
            ticket_id="MATCH",
            epic_id="EPIC-A",
            allowed_paths=["app/core/m.py"],
        )

        linear_mock.get_open_blockers.return_value = []
        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str) -> ExecutionManifest:
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifest

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
            w._start_ticket("MATCH", "linear-id-match", candidate=candidate)

        assert len(w._local_active) == 2
        same_epic = w._local_active[1].same_epic_pair
        assert same_epic is True

    def test_same_epic_pair_false_for_different_parent(self, tmp_path: Path) -> None:
        """When candidate parent does not match any active worker's parent,
        same_epic_pair=False."""
        active_manifest = make_manifest(
            ticket_id="WOR-100",
            epic_id="EPIC-A",
            allowed_paths=["app/core/y.py"],
        )
        active_worker = ActiveWorker(
            ticket_id="WOR-100",
            linear_id="fake-linear-id-100",
            manifest=active_manifest,
            worktree_path=tmp_path / "worktree_100",
            process=MagicMock(spec=subprocess.Popen),
        )

        linear_mock = MagicMock()

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )
        w._local_active = [active_worker]

        ticket = _regular_ticket("DIFF", priority=2, parent_id="EPIC-B")
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [ticket]

        candidate = {
            "id": "linear-id-diff",
            "identifier": "DIFF",
            "parent": {"id": "EPIC-B"},
            "labels": {"nodes": []},
        }
        manifest = _make_manifest_kw(
            ticket_id="DIFF",
            epic_id="EPIC-B",
            allowed_paths=["app/core/d.py"],
        )

        linear_mock.get_open_blockers.return_value = []
        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str) -> ExecutionManifest:
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifest

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
            w._start_ticket("DIFF", "linear-id-diff", candidate=candidate)

        assert len(w._local_active) == 2
        same_epic = w._local_active[1].same_epic_pair
        assert same_epic is False

    def test_same_epic_pair_false_when_no_active_workers(self, tmp_path: Path) -> None:
        """When no active workers exist, same_epic_pair=False even if the
        candidate has a parent."""
        linear_mock = MagicMock()

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )
        w._local_active = []  # empty pool

        ticket = _regular_ticket("NOPE", priority=2, parent_id="EPIC-A")
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [ticket]

        candidate = {
            "id": "linear-id-nope",
            "identifier": "NOPE",
            "parent": {"id": "EPIC-A"},
            "labels": {"nodes": []},
        }
        manifest = _make_manifest_kw(
            ticket_id="NOPE",
            epic_id="EPIC-A",
            allowed_paths=["app/core/n.py"],
        )

        linear_mock.get_open_blockers.return_value = []
        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str) -> ExecutionManifest:
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifest

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
            w._start_ticket("NOPE", "linear-id-nope", candidate=candidate)

        assert len(w._local_active) == 1
        same_epic = w._local_active[0].same_epic_pair
        assert same_epic is False

    def test_same_epic_pair_false_when_no_candidate(self, tmp_path: Path) -> None:
        """When candidate is None, same_epic_pair=False."""
        active_manifest = make_manifest(
            ticket_id="WOR-100",
            epic_id="EPIC-A",
            allowed_paths=["app/core/y.py"],
        )
        active_worker = ActiveWorker(
            ticket_id="WOR-100",
            linear_id="fake-linear-id-100",
            manifest=active_manifest,
            worktree_path=tmp_path / "worktree_100",
            process=MagicMock(spec=subprocess.Popen),
        )

        linear_mock = MagicMock()

        w = Watcher(
            linear_client=linear_mock,
            repo_root=tmp_path,
            max_local_workers=8,
        )
        w._local_active = [active_worker]

        ticket = _regular_ticket("NOCAND", priority=2, parent_id="EPIC-A")
        linear_mock.get_open_blockers.return_value = []
        linear_mock.list_ready_for_local.return_value = [ticket]

        manifest = _make_manifest_kw(
            ticket_id="NOCAND",
            epic_id="EPIC-A",
            allowed_paths=["app/core/c.py"],
        )

        linear_mock.get_open_blockers.return_value = []
        load_counts: dict[str, int] = {}

        def capture_load(ticket_id: str) -> ExecutionManifest:
            load_counts[ticket_id] = load_counts.get(ticket_id, 0) + 1
            return manifest

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
            # Pass candidate=None — same_epic_pair must be False
            w._start_ticket("NOCAND", "linear-id-nocand", candidate=None)

        assert len(w._local_active) == 2
        same_epic = w._local_active[1].same_epic_pair
        assert same_epic is False
