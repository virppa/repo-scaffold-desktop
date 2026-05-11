"""Tests for app.core.manifest_builder.

Covers the WOR-382 acceptance criteria:
- build_manifest() returns a dict matching ExecutionManifest schema
- write_manifest() validates via Pydantic and writes to disk
- Round-trip regression: re-running the WOR-313 generator produces
  byte-identical existing manifests under .claude/artifacts/wor_*/.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest

from app.core.manifest import ExecutionManifest
from app.core.manifest_builder import (
    TaxonomyFields,
    build_manifest,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _minimal_kwargs(**overrides: object) -> dict[str, object]:
    """Smallest valid build_manifest call; used by several tests."""
    base: dict[str, object] = {
        "ticket_id": "WOR-999",
        "epic_id": "WOR-313",
        "branch": "wor-999-test",
        "title": "Test manifest",
        "allowed_paths": ["app/core/foo.py"],
        "related_files_hint": ["app/core/bar.py"],
        "effort": "high",
        "change_type": "additive",
        "taxonomy": TaxonomyFields(
            reasoning_demand=2,
            scope_clarity=4,
            constraint_density=3,
            ac_specificity=4,
        ),
        "objective": "Test that build_manifest works.",
        "acceptance_criteria": ["AC 1", "AC 2"],
        "implementation_constraints": ["Do the thing"],
        "tech_stack": "python",
        "raw_extensions": [".py"],
    }
    base.update(overrides)
    return base


def test_build_manifest_returns_pydantic_valid_dict() -> None:
    """build_manifest output must validate against ExecutionManifest."""
    out = build_manifest(**_minimal_kwargs())
    # Validate via the canonical model — same path write_manifest takes.
    ExecutionManifest.model_validate(out)


def test_build_manifest_artifact_paths_use_underscored_id() -> None:
    """ticket_id hyphens normalize to underscores in artifact paths."""
    out = build_manifest(**_minimal_kwargs(ticket_id="WOR-123"))
    assert out["artifact_paths"]["result_json"] == (
        ".claude/artifacts/wor_123/result.json"
    )
    assert out["artifact_paths"]["manifest_copy"] == (
        ".claude/artifacts/wor_123/manifest.json"
    )


def test_build_manifest_forbidden_paths_excludes_allowed_overlap() -> None:
    """If an allowed_path matches a common-forbidden entry, it is not also forbidden."""
    out = build_manifest(
        **_minimal_kwargs(allowed_paths=[".env", "app/core/foo.py"])
    )
    forbidden = out["forbidden_paths"]
    assert ".env" not in forbidden
    assert ".mcp.json" in forbidden  # Other common-forbidden entries survive.


def test_build_manifest_forbidden_paths_extra_appends() -> None:
    """forbidden_paths_extra entries appear in the output forbidden list."""
    out = build_manifest(
        **_minimal_kwargs(forbidden_paths_extra=["scripts/sensitive.py"])
    )
    assert "scripts/sensitive.py" in out["forbidden_paths"]


def test_write_manifest_writes_indented_json(tmp_path: Path) -> None:
    """write_manifest writes pretty-printed JSON and round-trips via Pydantic."""
    out = build_manifest(**_minimal_kwargs())
    target = tmp_path / "wor_999" / "manifest.json"
    written = write_manifest(target, out)
    assert written == target
    text = target.read_text(encoding="utf-8")
    assert text.startswith("{\n")  # Indented.
    loaded = json.loads(text)
    assert loaded["ticket_id"] == "WOR-999"
    ExecutionManifest.model_validate(loaded)


def test_write_manifest_validate_false_skips_pydantic(tmp_path: Path) -> None:
    """validate=False writes the dict as-is even if it fails Pydantic."""
    target = tmp_path / "wor_999" / "manifest.json"
    bad: dict[str, object] = {"ticket_id": "WOR-999", "missing_required": True}
    write_manifest(target, bad, validate=False)
    assert json.loads(target.read_text(encoding="utf-8")) == bad


def test_wor313_round_trip_byte_identical() -> None:
    """Regression guard: re-running the WOR-313 generator produces no diff.

    The .claude/artifacts/wor_*/manifest.json files for the WOR-313 ticket
    set are the source of truth. If build_manifest's output changes shape
    (field renames, ordering, defaults), this test catches it.
    """
    script = REPO_ROOT / "scripts" / "generate_overnight_manifests.py"
    if not script.exists():
        pytest.skip("generate_overnight_manifests.py not present")

    artifact_dir = REPO_ROOT / ".claude" / "artifacts"
    if not artifact_dir.exists():
        pytest.skip("no existing artifacts to diff against")

    # Run the generator; it overwrites existing manifests in place.
    result = subprocess.run(  # nosec B603 B607
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"generator failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Check that .claude/artifacts/ has no uncommitted diff after regeneration.
    diff = subprocess.run(  # nosec B603 B607
        ["git", "diff", "--quiet", "--", ".claude/artifacts/"],
        cwd=REPO_ROOT,
        timeout=30,
    )
    if diff.returncode != 0:
        # Show what changed for debuggability.
        show = subprocess.run(  # nosec B603 B607
            ["git", "diff", "--stat", "--", ".claude/artifacts/"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        pytest.fail(
            "Regenerated manifests differ from committed ones:\n"
            + show.stdout
        )
