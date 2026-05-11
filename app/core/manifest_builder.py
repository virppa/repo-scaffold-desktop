"""Build ExecutionManifest JSON from ticket data.

Public API
----------
build_manifest(...) -> dict[str, Any]  -- construct the manifest dict
write_manifest(path, manifest_dict)    -- write + validate to disk
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.manifest import ExecutionManifest

COMMON_FORBIDDEN = [
    ".env",
    ".mcp.json",
    ".claude/settings*",
]


class TaxonomyFields:
    """Bundle for the 5 taxonomy knobs on a ticket.

    All values are clamped to 1-5 integers by the pydantic model;
    no validation is done here so the manifest schema remains the
    single source of truth.
    """

    __slots__ = (
        "reasoning_demand",
        "scope_clarity",
        "constraint_density",
        "ac_specificity",
    )

    def __init__(
        self,
        reasoning_demand: int = 1,
        scope_clarity: int = 1,
        constraint_density: int = 1,
        ac_specificity: int = 1,
    ) -> None:
        self.reasoning_demand = reasoning_demand
        self.scope_clarity = scope_clarity
        self.constraint_density = constraint_density
        self.ac_specificity = ac_specificity


@dataclass
class ManifestOptions:
    """Optional/defaulted kwargs for ``build_manifest`` (WOR-Sonar S107).

    Bundles the rarely-set knobs so the main builder signature stays under
    the 13-parameter Sonar threshold.
    """

    tech_stack: str = ""
    raw_extensions: list[str] = field(default_factory=list)
    forbidden_paths_extra: list[str] = field(default_factory=list)
    risk_level: str = "low"
    priority: int = 3


class ManifestBuilder:
    """Builder for ExecutionManifest dicts."""

    @staticmethod
    def _compute_forbidden_paths(
        allowed_paths: list[str],
        forbidden_extra: list[str],
    ) -> list[str]:
        allowed_set = set(allowed_paths)
        return [p for p in COMMON_FORBIDDEN + forbidden_extra if p not in allowed_set]

    @staticmethod
    def _artifact_id(ticket_id: str) -> str:
        return ticket_id.lower().replace("-", "_")

    def build_manifest(
        self,
        *,
        ticket_id: str,
        epic_id: str,
        branch: str,
        title: str,
        allowed_paths: list[str],
        related_files_hint: list[str],
        effort: str,
        change_type: str,
        taxonomy: TaxonomyFields,
        objective: str,
        acceptance_criteria: list[str],
        implementation_constraints: list[str],
        options: ManifestOptions | None = None,
    ) -> dict[str, Any]:
        """Return a dict matching ExecutionManifest schema."""
        opts = options if options is not None else ManifestOptions()

        forbidden_paths = self._compute_forbidden_paths(
            allowed_paths,
            opts.forbidden_paths_extra,
        )
        artifact_id = self._artifact_id(ticket_id)

        return {
            "manifest_version": "1.0",
            "ticket_id": ticket_id,
            "epic_id": epic_id,
            "title": title,
            "priority": opts.priority,
            "status": "ReadyForLocal",
            "linear_id": None,
            "blocked_by_tickets": [],
            "parallel_safe": True,
            "risk_level": opts.risk_level,
            "risk_flags": [],
            "implementation_mode": "local",
            "effort": effort,
            "review_mode": "auto",
            "change_type": change_type,
            "reasoning_demand": taxonomy.reasoning_demand,
            "scope_clarity": taxonomy.scope_clarity,
            "constraint_density": taxonomy.constraint_density,
            "ac_specificity": taxonomy.ac_specificity,
            "tech_stack": opts.tech_stack,
            "raw_extensions": json.dumps(opts.raw_extensions),
            "base_branch": "epic/wor-313-mega-overnight-hardening",
            "worker_branch": branch,
            "worktree_name": None,
            "objective": objective,
            "acceptance_criteria": acceptance_criteria,
            "implementation_constraints": implementation_constraints,
            "allowed_paths": allowed_paths,
            "forbidden_paths": forbidden_paths,
            "related_files_hint": related_files_hint,
            "required_checks": ["ruff check .", "mypy app/", "pytest"],
            "optional_checks": [],
            "done_definition": objective[:200],
            "failure_policy": {
                "on_check_failure": "abort",
                "max_retries": 0,
                "escalate_to_cloud": False,
            },
            "ticket_state_map": {
                "in_progress_local": "InProgressLocal",
                "failed": "Blocked",
            },
            "context_snippets": [],
            "artifact_paths": {
                "result_json": f".claude/artifacts/{artifact_id}/result.json",
                "manifest_copy": f".claude/artifacts/{artifact_id}/manifest.json",
            },
        }


def build_manifest(
    *,
    ticket_id: str,
    epic_id: str,
    branch: str,
    title: str,
    allowed_paths: list[str],
    related_files_hint: list[str],
    effort: str,
    change_type: str,
    taxonomy: TaxonomyFields,
    objective: str,
    acceptance_criteria: list[str],
    implementation_constraints: list[str],
    options: ManifestOptions | None = None,
) -> dict[str, Any]:
    """Convenience function -- delegates to a singleton builder."""
    return ManifestBuilder().build_manifest(
        ticket_id=ticket_id,
        epic_id=epic_id,
        branch=branch,
        title=title,
        allowed_paths=allowed_paths,
        related_files_hint=related_files_hint,
        effort=effort,
        change_type=change_type,
        taxonomy=taxonomy,
        objective=objective,
        acceptance_criteria=acceptance_criteria,
        implementation_constraints=implementation_constraints,
        options=options,
    )


def write_manifest(
    path: Path,
    manifest_dict: dict[str, Any],
    *,
    validate: bool = True,
) -> Path:
    """Write manifest_dict to path and optionally validate."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    if validate:
        ExecutionManifest.model_validate(manifest_dict)

    path.write_text(
        json.dumps(manifest_dict, indent=2),
        encoding="utf-8",
    )
    return path
