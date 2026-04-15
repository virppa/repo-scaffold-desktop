"""Tests for the ExecutionManifest schema (WOR-77)."""

import json

import pytest
from pydantic import ValidationError

from app.core.manifest import (
    MANIFEST_VERSION,
    ArtifactPaths,
    ExecutionManifest,
    TicketStateMap,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_manifest(**overrides) -> dict:
    """Return the smallest valid manifest payload."""
    base = {
        "ticket_id": "WOR-77",
        "title": "Design manifest schema",
        "priority": 2,
        "status": "ReadyForLocal",
        "parallel_safe": True,
        "risk_level": "low",
        "implementation_mode": "local",
        "review_mode": "auto",
        "base_branch": "wor-75-hybrid-execution-engine",
        "worker_branch": "wor-77-design-and-implement-execution-manifest-schema",
        "objective": "Define the execution manifest schema.",
        "artifact_paths": {
            "result_json": ".claude/artifacts/wor_77/result.json",
            "manifest_copy": ".claude/artifacts/wor_77/manifest.json",
        },
    }
    base.update(overrides)
    return base


def _make_manifest(**overrides) -> ExecutionManifest:
    return ExecutionManifest(**_minimal_manifest(**overrides))


# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------


def test_manifest_constructs_from_minimal_payload():
    m = _make_manifest()
    assert m.ticket_id == "WOR-77"
    assert m.manifest_version == MANIFEST_VERSION


def test_manifest_version_default():
    m = _make_manifest()
    assert m.manifest_version == "1.0"


def test_optional_fields_default_to_empty_or_none():
    m = _make_manifest()
    assert m.epic_id is None
    assert m.risk_flags == []
    assert m.acceptance_criteria == []
    assert m.implementation_constraints == []
    assert m.allowed_paths == []
    assert m.forbidden_paths == []
    assert m.related_files_hint == []
    assert m.required_checks == []
    assert m.optional_checks == []
    assert m.done_definition == ""
    assert m.worktree_name is None


def test_ticket_state_map_default_values():
    m = _make_manifest()
    assert m.ticket_state_map.in_progress_local == "InProgressLocal"
    assert m.ticket_state_map.merged_to_epic == "MergedToEpic"


def test_failure_policy_default_abort():
    m = _make_manifest()
    assert m.failure_policy.on_check_failure == "abort"
    assert m.failure_policy.max_retries == 0
    assert m.failure_policy.escalate_to_cloud is False


# ---------------------------------------------------------------------------
# Validation — required fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    [
        "ticket_id",
        "title",
        "priority",
        "status",
        "parallel_safe",
        "risk_level",
        "implementation_mode",
        "review_mode",
        "base_branch",
        "worker_branch",
        "objective",
        "artifact_paths",
    ],
)
def test_manifest_missing_required_field_raises(missing_field):
    payload = _minimal_manifest()
    del payload[missing_field]
    with pytest.raises(ValidationError):
        ExecutionManifest(**payload)


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def test_manifest_version_mismatch_raises():
    with pytest.raises(ValidationError, match="Unsupported manifest_version"):
        _make_manifest(manifest_version="0.9")


def test_ticket_id_normalised_to_uppercase():
    m = _make_manifest(ticket_id="wor-77")
    assert m.ticket_id == "WOR-77"


def test_epic_id_normalised_to_uppercase():
    m = _make_manifest(epic_id="wor-75")
    assert m.epic_id == "WOR-75"


def test_epic_id_none_is_valid():
    m = _make_manifest(epic_id=None)
    assert m.epic_id is None


def test_priority_bounds():
    _make_manifest(priority=0)
    _make_manifest(priority=4)
    with pytest.raises(ValidationError):
        _make_manifest(priority=-1)
    with pytest.raises(ValidationError):
        _make_manifest(priority=5)


def test_risk_level_valid_values():
    for level in ("low", "medium", "high"):
        m = _make_manifest(risk_level=level)
        assert m.risk_level == level


def test_risk_level_invalid_raises():
    with pytest.raises(ValidationError):
        _make_manifest(risk_level="critical")


def test_implementation_mode_valid_values():
    for mode in ("local", "cloud", "hybrid"):
        m = _make_manifest(implementation_mode=mode)
        assert m.implementation_mode == mode


def test_review_mode_valid_values():
    for mode in ("auto", "human"):
        m = _make_manifest(review_mode=mode)
        assert m.review_mode == mode


def test_allowed_paths_empty_list_is_valid():
    m = _make_manifest(allowed_paths=[])
    assert m.allowed_paths == []


def test_required_checks_rejects_empty_strings():
    with pytest.raises(ValidationError, match="must not be empty"):
        _make_manifest(required_checks=["pytest", ""])


def test_optional_checks_rejects_empty_strings():
    with pytest.raises(ValidationError, match="must not be empty"):
        _make_manifest(optional_checks=[""])


def test_allowed_forbidden_overlap_raises():
    with pytest.raises(ValidationError, match="both allowed_paths and forbidden_paths"):
        _make_manifest(
            allowed_paths=["app/core/*.py"],
            forbidden_paths=["app/core/*.py"],
        )


def test_allowed_forbidden_no_overlap_is_valid():
    m = _make_manifest(
        allowed_paths=["app/core/*.py"],
        forbidden_paths=["app/ui/*.py"],
    )
    assert "app/core/*.py" in m.allowed_paths
    assert "app/ui/*.py" in m.forbidden_paths


# ---------------------------------------------------------------------------
# Extra fields rejected
# ---------------------------------------------------------------------------


def test_manifest_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ExecutionManifest(**_minimal_manifest(unknown_future_field="oops"))


def test_ticket_state_map_rejects_extra_fields():
    with pytest.raises(ValidationError):
        TicketStateMap(in_progress_local="X", surprise="Y")


def test_artifact_paths_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ArtifactPaths(
            result_json=".claude/artifacts/x/result.json",
            manifest_copy=".claude/artifacts/x/manifest.json",
            extra="bad",
        )


# ---------------------------------------------------------------------------
# ArtifactPaths helpers
# ---------------------------------------------------------------------------


def test_artifact_paths_from_ticket_id():
    ap = ArtifactPaths.from_ticket_id("WOR-77")
    assert ap.result_json == ".claude/artifacts/wor_77/result.json"
    assert ap.manifest_copy == ".claude/artifacts/wor_77/manifest.json"


def test_artifact_paths_no_traversal():
    with pytest.raises(ValidationError, match="must not contain"):
        ArtifactPaths(
            result_json="../secrets/result.json",
            manifest_copy=".claude/artifacts/x/manifest.json",
        )


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------


def test_manifest_roundtrip_json_string():
    m = _make_manifest(
        epic_id="WOR-75",
        risk_flags=["touches .claude/"],
        acceptance_criteria=["Schema validates", "Tests pass"],
        required_checks=["pytest tests/test_manifest.py"],
    )
    json_str = m.model_dump_json()
    m2 = ExecutionManifest.model_validate_json(json_str)
    assert m == m2


def test_manifest_roundtrip_dict():
    m = _make_manifest()
    d = m.model_dump()
    m2 = ExecutionManifest.model_validate(d)
    assert m == m2


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def test_to_json_creates_file(tmp_path):
    m = _make_manifest()
    dest = tmp_path / "manifests" / "result.json"
    written = m.to_json(dest)
    assert written == dest
    assert dest.exists()
    loaded = json.loads(dest.read_text())
    assert loaded["ticket_id"] == "WOR-77"
    assert loaded["manifest_version"] == MANIFEST_VERSION


def test_from_json_roundtrip(tmp_path):
    m = _make_manifest(epic_id="WOR-75")
    dest = tmp_path / "manifest.json"
    m.to_json(dest)
    m2 = ExecutionManifest.from_json(dest)
    assert m == m2


def test_from_json_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        ExecutionManifest.from_json(tmp_path / "nonexistent.json")


def test_from_json_traversal_guard(tmp_path):
    with pytest.raises(ValueError, match="must not contain"):
        ExecutionManifest.from_json("some/../../etc/passwd")


def test_from_json_invalid_schema(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        '{"ticket_id": "WOR-1"}', encoding="utf-8"
    )  # missing required fields
    with pytest.raises(ValidationError):
        ExecutionManifest.from_json(bad)


# ---------------------------------------------------------------------------
# JSON Schema export
# ---------------------------------------------------------------------------


def test_json_schema_is_dict():
    schema = ExecutionManifest.json_schema()
    assert isinstance(schema, dict)
    assert schema.get("title") == "ExecutionManifest"


def test_json_schema_contains_required_properties():
    schema = ExecutionManifest.json_schema()
    props = schema.get("properties", {})
    for field in ("ticket_id", "title", "priority", "base_branch", "worker_branch"):
        assert field in props, f"Expected {field!r} in JSON Schema properties"
