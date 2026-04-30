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
# Known tech-stack literals (from file extensions)
# ---------------------------------------------------------------------------

_EXTENSION_TO_TECH_STACK: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".toml": "yaml_toml",
    ".yaml": "yaml_toml",
    ".yml": "yaml_toml",
    ".json": "json",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".scss": "css",
    ".sql": "sql",
    ".md": "markdown",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
}


# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class TaskProfile(BaseModel):
    """Work dimensions for a ticket — collected at plan time for routing calibration."""

    model_config = {"extra": "forbid"}

    change_type: Literal[
        "bugfix",
        "feature",
        "refactor",
        "api_integration",
        "architectural",
        "test",
        "docs",
    ]
    """What kind of change this ticket is."""

    reasoning_demand: Literal["mechanical", "analytical", "design"]
    """Cognitive load: mechanical (repetitive), analytical
    (reasoning), design (creative)."""

    scope_clarity: Literal["specified", "inferred", "ambiguous"]
    """How clearly the scope is defined."""

    constraint_density: Literal["low", "medium", "high"]
    """How many non-functional constraints (e.g. framework version, API contract)."""

    ac_specificity: Literal["testable", "behavioral", "vague"]
    """How specific the acceptance criteria are."""

    multi_file_consistency_required: bool
    """True if the ticket requires consistent changes across multiple files."""

    is_greenfield: bool
    """True if >70% of allowed_paths do not exist (new file creation)."""

    has_external_dependency: bool
    """True if any allowed_path suggests an external dependency
    (mcp, http, client, litellm)."""

    tech_stack: list[str]
    """Detected tech stack from file extensions, e.g. ['python', 'toml']."""

    raw_extensions: list[str]
    """Raw file extensions found in allowed_paths, e.g. ['.py', '.toml']."""

    def __str__(self) -> str:
        return (
            f"TaskProfile(change_type={self.change_type!r}, "
            f"reasoning_demand={self.reasoning_demand!r}, "
            f"scope_clarity={self.scope_clarity!r}, "
            f"constraint_density={self.constraint_density!r}, "
            f"ac_specificity={self.ac_specificity!r}, "
            f"multi_file_consistency_required={self.multi_file_consistency_required}, "
            f"is_greenfield={self.is_greenfield}, "
            f"has_external_dependency={self.has_external_dependency}, "
            f"tech_stack={self.tech_stack}, "
            f"raw_extensions={self.raw_extensions})"
        )


# ---------------------------------------------------------------------------
# Deterministic inference (pure — no LLM, no I/O)
# ---------------------------------------------------------------------------


def infer_is_greenfield(
    allowed_paths: list[str], existing_paths: set[str] | None = None
) -> bool:
    """Compute is_greenfield: True when >70% of allowed_paths do not exist.

    Parameters
    ----------
    allowed_paths:
        The allowed_paths list from the manifest.
    existing_paths:
        Optional set of paths that *do* exist on disk. If None, a simulated
        "new" set is used where every 5th path is considered existing — just
        enough to make the pure function testable without real filesystem I/O.

    Note: This is a *deterministic* inference rule. The real "existing" check
    would require filesystem I/O at runtime; here we provide a mockable
    existing_paths to keep the function pure and testable.
    """
    if not allowed_paths:
        return False

    if existing_paths is None:
        # Default deterministic mock: every 5th path "exists"
        existing_paths = {p for i, p in enumerate(allowed_paths) if i % 5 == 0}

    non_existing = sum(1 for p in allowed_paths if p not in existing_paths)
    return (non_existing / len(allowed_paths)) > 0.7


def infer_has_external_dependency(allowed_paths: list[str]) -> bool:
    """Return True if any allowed_path matches external-dependency patterns.

    Patterns: *http*, *mcp*, *litellm*, *client* (case-insensitive substring match).
    """
    if not allowed_paths:
        return False

    markers = ("http", "mcp", "litellm", "client")
    return any(marker in p.lower() for p in allowed_paths for marker in markers)


def infer_tech_stack(allowed_paths: list[str]) -> list[str]:
    """Detect tech stack from file extensions in allowed_paths.

    Returns a deduplicated list of known tech-stack literals, preserving
    insertion order (first extension wins if multiple files share an ext).
    """
    stack: list[str] = []
    seen: set[str] = set()
    for path in allowed_paths:
        ext = Path(path).suffix
        tech = _EXTENSION_TO_TECH_STACK.get(ext)
        if tech and tech not in seen:
            stack.append(tech)
            seen.add(tech)
    return stack


def infer_raw_extensions(allowed_paths: list[str]) -> list[str]:
    """Extract raw file extensions from allowed_paths, preserving order."""
    exts: list[str] = []
    seen: set[str] = set()
    for path in allowed_paths:
        ext = Path(path).suffix
        if ext and ext not in seen:
            exts.append(ext)
            seen.add(ext)
    return exts


# ---------------------------------------------------------------------------
# Helper: Build a TaskProfile from allowed_paths + LLM-assessed fields
# ---------------------------------------------------------------------------


def build_task_profile(
    change_type: Literal[
        "bugfix",
        "feature",
        "refactor",
        "api_integration",
        "architectural",
        "test",
        "docs",
    ],
    reasoning_demand: Literal["mechanical", "analytical", "design"],
    scope_clarity: Literal["specified", "inferred", "ambiguous"],
    constraint_density: Literal["low", "medium", "high"],
    ac_specificity: Literal["testable", "behavioral", "vague"],
    multi_file_consistency_required: bool,
    allowed_paths: list[str],
    existing_paths: set[str] | None = None,
) -> TaskProfile:
    """Convenience function to build a TaskProfile deterministically.

    The caller supplies the 5 LLM-assessed Literal fields; the rest are
    computed from allowed_paths via pure inference.

    This function validates the LLM-assessed inputs through the Pydantic
    model — unknown values will raise ValidationError.
    """
    return TaskProfile(
        change_type=change_type,
        reasoning_demand=reasoning_demand,
        scope_clarity=scope_clarity,
        constraint_density=constraint_density,
        ac_specificity=ac_specificity,
        multi_file_consistency_required=multi_file_consistency_required,
        is_greenfield=infer_is_greenfield(allowed_paths, existing_paths),
        has_external_dependency=infer_has_external_dependency(allowed_paths),
        tech_stack=infer_tech_stack(allowed_paths),
        raw_extensions=infer_raw_extensions(allowed_paths),
    )


class TicketStateMap(BaseModel):
    """Linear workflow state names used by the watcher to advance ticket state."""

    model_config = {"extra": "forbid"}

    in_progress_local: str = "InProgressLocal"
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

    context_snippets: list[str] | None = None
    """Pre-injected code snippets from the cloud producer. Each entry is a
    verbatim excerpt (file path + lines) the worker should treat as already-read.
    Reduces full-file reads and saves context window for implementation."""

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
    # Dependency tracking (WaitingForDeps promotion)
    # ------------------------------------------------------------------
    linear_id: str | None = None
    """Linear UUID for this ticket (not the WOR-XX human identifier).
    Required when status == 'WaitingForDeps' so the watcher can call
    set_state without a prior Linear poll."""

    blocked_by_tickets: list[str] = Field(default_factory=list)
    """Human identifiers (e.g. ['WOR-45']) of tickets that must reach a
    Linear completed/cancelled state before this manifest is promoted to
    ReadyForLocal. Only meaningful when status == 'WaitingForDeps'."""

    # ------------------------------------------------------------------
    # State mapping and artifacts
    # ------------------------------------------------------------------
    ticket_state_map: TicketStateMap = Field(default_factory=TicketStateMap)

    artifact_paths: ArtifactPaths
    """Where the worker writes its result. Use ArtifactPaths.from_ticket_id()."""

    task_profile: TaskProfile | None = None
    """Work dimensions captured at plan time for routing calibration (WOR-216).
    None when not set — old manifests without this field still validate."""

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
