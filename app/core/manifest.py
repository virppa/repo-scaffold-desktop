"""Execution manifest schema.

The ExecutionManifest is the machine-readable contract written by the cloud
producer (/start-ticket) and consumed by the local worker (/implement-ticket).
The worker executes what the manifest specifies — it does not re-read Linear,
re-interpret the project, or make architectural decisions on its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MANIFEST_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class TicketStateMap(BaseModel):
    """Linear workflow state names used by the watcher to advance ticket state."""

    model_config = {"extra": "forbid"}

    in_progress_local: str = "InProgressLocal"
    in_review: str = "In Review"
    merged_to_epic: str = "MergedToEpic"
    ready_for_review: str = "EpicReadyForCloudReview"
    failed: str = "Blocked"


class ArtifactPaths(BaseModel):
    """Paths where the worker writes result artifacts (relative to repo root)."""

    model_config = {"extra": "forbid"}

    result_json: str
    """JSON file the worker writes on completion: {status, summary, checks_passed}."""

    manifest_copy: str
    """Copy of the manifest as executed — preserves the exact contract for audit."""

    @classmethod
    def from_ticket_id(cls, ticket_id: str) -> "ArtifactPaths":
        """Generate default artifact paths for a given ticket ID."""
        slug = ticket_id.lower().replace("-", "_")
        base = f".claude/artifacts/{slug}"
        return cls(
            result_json=f"{base}/result.json",
            manifest_copy=f"{base}/manifest.json",
        )

    @field_validator("result_json", "manifest_copy")
    @classmethod
    def no_path_traversal(cls, v: str) -> str:
        resolved = Path(v)
        if ".." in resolved.parts:
            raise ValueError(f"Path must not contain '..': {v!r}")
        return v


class FailurePolicy(BaseModel):
    """What the worker should do when a required check fails."""

    model_config = {"extra": "forbid"}

    on_check_failure: Literal["abort", "warn"] = "abort"
    """abort — stop immediately and write a failed result artifact.
    warn  — log the failure but continue (use only for non-blocking checks)."""

    max_retries: Annotated[int, Field(ge=0, le=5)] = 0
    """Number of times the worker may retry a failed required check before giving up."""

    escalate_to_cloud: bool = False
    """If True the watcher should escalate to a cloud session when the worker fails."""


# ---------------------------------------------------------------------------
# Root manifest model
# ---------------------------------------------------------------------------


class ExecutionManifest(BaseModel):
    """Full execution manifest — contract between cloud producer and local worker."""

    model_config = {"extra": "forbid"}

    # ------------------------------------------------------------------
    # Schema version
    # ------------------------------------------------------------------
    manifest_version: str = MANIFEST_VERSION
    """Semver-style schema version. Must match what the worker supports."""

    # ------------------------------------------------------------------
    # Ticket identity
    # ------------------------------------------------------------------
    ticket_id: str
    """Linear ticket identifier, e.g. 'WOR-77'."""

    epic_id: str | None = None
    """Linear epic identifier, e.g. 'WOR-75'. None for top-level (non-epic) tickets."""

    title: str
    """Human-readable ticket title copied from Linear."""

    priority: Annotated[int, Field(ge=0, le=4)]
    """Linear priority: 0=None, 1=Urgent, 2=High, 3=Normal, 4=Low."""

    status: str
    """Linear status at manifest generation time, e.g. 'ReadyForLocal'."""

    # ------------------------------------------------------------------
    # Execution control
    # ------------------------------------------------------------------
    parallel_safe: bool
    """True if this ticket can run concurrently with other local workers.
    The watcher enforces allowed_paths non-overlap; this flag is advisory."""

    risk_level: Literal["low", "medium", "high"]
    """Overall risk classification assigned by the cloud producer."""

    risk_flags: list[str] = Field(default_factory=list)
    """Specific risk notes, e.g. ['touches migrations', 'modifies CI config']."""

    implementation_mode: Literal["local", "cloud", "hybrid"]
    """Which execution mode this manifest is targeting."""

    review_mode: Literal["auto", "human"]
    """auto — PR auto-merges to epic branch when CI passes.
    human — PR requires explicit human approval."""

    # ------------------------------------------------------------------
    # Branch / worktree
    # ------------------------------------------------------------------
    base_branch: str
    """Branch the worker bases its work on, e.g. 'wor-75-hybrid-execution-engine'."""

    worker_branch: str
    """Branch the worker commits to, e.g. 'wor-77-design-and-implement-...'."""

    worktree_name: str | None = None
    """Git worktree directory name. None when not using isolated worktrees."""

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------
    objective: str
    """One-paragraph description of what the worker must accomplish."""

    acceptance_criteria: list[str] = Field(default_factory=list)
    """Bullet list of conditions that must be true for the ticket to be Done."""

    implementation_constraints: list[str] = Field(default_factory=list)
    """Hard rules the worker must follow, e.g. 'do not modify app/ui/'."""

    allowed_paths: list[str] = Field(default_factory=list)
    """Glob patterns the worker is allowed to write to. Empty list = no restriction."""

    forbidden_paths: list[str] = Field(default_factory=list)
    """Glob patterns the worker must never write to."""

    related_files_hint: list[str] = Field(default_factory=list)
    """Files likely relevant to this ticket (informational, not a whitelist)."""

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    required_checks: list[str] = Field(default_factory=list)
    """Shell commands that must pass before the worker marks the ticket done.
    E.g. ['pytest tests/test_manifest.py', 'ruff check app/core/manifest.py']."""

    optional_checks: list[str] = Field(default_factory=list)
    """Shell commands run for information only — failures do not block completion."""

    # ------------------------------------------------------------------
    # Done definition and failure policy
    # ------------------------------------------------------------------
    done_definition: str = ""
    """Plain-English description of what 'Done' means for this ticket."""

    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)

    # ------------------------------------------------------------------
    # State mapping and artifacts
    # ------------------------------------------------------------------
    ticket_state_map: TicketStateMap = Field(default_factory=TicketStateMap)

    artifact_paths: ArtifactPaths
    """Where the worker writes its result. Use ArtifactPaths.from_ticket_id()."""

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("manifest_version")
    @classmethod
    def version_must_be_supported(cls, v: str) -> str:
        if v != MANIFEST_VERSION:
            raise ValueError(
                f"Unsupported manifest_version {v!r}. "
                f"This implementation supports {MANIFEST_VERSION!r}."
            )
        return v

    @field_validator("ticket_id", "epic_id")
    @classmethod
    def identifier_format(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("Identifier must not be blank")
        return v.strip().upper()

    @field_validator("required_checks", "optional_checks")
    @classmethod
    def no_empty_check_strings(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item.strip():
                raise ValueError("Check commands must not be empty strings")
        return v

    @model_validator(mode="after")
    def forbidden_not_subset_of_allowed(self) -> "ExecutionManifest":
        """Warn (via ValueError) when a forbidden path is also explicitly allowed."""
        if self.allowed_paths and self.forbidden_paths:
            overlap = set(self.allowed_paths) & set(self.forbidden_paths)
            if overlap:
                raise ValueError(
                    f"Paths appear in both allowed_paths and forbidden_paths: {overlap}"
                )
        return self

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_json(self, path: str | Path, *, indent: int = 2) -> Path:
        """Serialize manifest to a JSON file. Creates parent directories as needed."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.model_dump_json(indent=indent), encoding="utf-8")
        return dest

    @classmethod
    def from_json(cls, path: str | Path) -> "ExecutionManifest":
        """Load and validate a manifest from a JSON file.

        Raises ValueError if the path contains '..' (traversal guard).
        Raises FileNotFoundError if the file does not exist.
        Raises pydantic.ValidationError if the JSON fails schema validation.
        """
        resolved = Path(path).resolve()
        # Traversal guard: reject paths that escape via '..'
        if ".." in Path(path).parts:
            raise ValueError(f"Manifest path must not contain '..': {path!r}")
        raw = resolved.read_text(encoding="utf-8")
        return cls.model_validate_json(raw)

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Return the JSON Schema dict for this model."""
        return cls.model_json_schema()
