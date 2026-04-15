# WOR-77 Spike: Execution Manifest Schema

**Milestone:** Hybrid Execution Engine
**Status:** Done
**Blocks:** WOR-80 (Split /start-ticket into preflight + /implement-ticket skill)

---

## What is the execution manifest?

The execution manifest is a machine-readable JSON file that acts as the **handoff contract** between two components of the hybrid execution engine:

- **Cloud producer** — the updated `/start-ticket` skill running on Claude Sonnet/Opus. It reads Linear, interprets the project, makes architectural decisions, and writes a manifest to disk.
- **Local worker** — the future `/implement-ticket` skill running on a local model (Qwen3-Coder or similar). It reads the manifest and executes it **without re-interpreting the project**.

The manifest carries everything the local worker needs. The worker is intentionally dumb: it follows the manifest, runs the required checks, and writes a result artifact. It does not re-read Linear or consult CLAUDE.md.

---

## Design decisions

### Why Pydantic?

The rest of `app/core/` uses Pydantic (see `config.py`). Pydantic v2 gives us:
- Free JSON serialization / deserialization
- Auto-generated JSON Schema (committed as `schemas/execution_manifest.schema.json`)
- Strict validation with `extra = "forbid"` — unknown fields fail loudly rather than silently disappearing

### Why `extra = "forbid"` on all models?

Manifest version mismatches should fail loudly. If a future field is added, old workers will reject the manifest rather than silently ignore the new constraint. The producer must bump `manifest_version` when the schema changes in a breaking way.

### Why `manifest_version` validation instead of a version range?

For v1 of this system we have exactly one worker implementation. A range check would be premature generalization. When we need backwards compatibility we will revisit.

### `allowed_paths` empty list = no restriction

An empty `allowed_paths` list means the worker is not path-restricted. This is intentional for spike/research tickets where the scope is exploratory. The watcher checks that two concurrent workers' `allowed_paths` don't overlap — an empty list means "potentially touches anything" and the watcher must treat it conservatively (serialize it, don't allow concurrency).

### Path traversal guard in `from_json`

`ArtifactPaths` validates individual paths at construction time. `from_json` adds a second check at the file-loading layer. Defense in depth — the manifest file itself might come from an untrusted source in future.

### `ticket_state_map` is configurable

Linear state names are project-specific and may differ across repos. The map defaults to the names defined in WOR-78 but can be overridden per-manifest, making the schema portable.

---

## Field reference

### Identity

| Field | Type | Required | Description |
|---|---|---|---|
| `manifest_version` | `str` | default `"1.0"` | Schema version. Must match `MANIFEST_VERSION` constant. |
| `ticket_id` | `str` | yes | Linear identifier, e.g. `"WOR-77"`. Normalized to uppercase. |
| `epic_id` | `str \| null` | no | Parent epic identifier. `null` for standalone tickets. |
| `title` | `str` | yes | Ticket title from Linear. |
| `priority` | `int` 0–4 | yes | Linear priority (0=None … 4=Low). |
| `status` | `str` | yes | Linear status at manifest generation time. |

### Execution control

| Field | Type | Required | Description |
|---|---|---|---|
| `parallel_safe` | `bool` | yes | Advisory flag. Watcher enforces concurrency via `allowed_paths`. |
| `risk_level` | `"low"\|"medium"\|"high"` | yes | Overall risk classification. Drives review mode and escalation. |
| `risk_flags` | `list[str]` | no | Specific risk notes, e.g. `["touches migrations"]`. |
| `implementation_mode` | `"local"\|"cloud"\|"hybrid"` | yes | Target execution mode for this manifest. |
| `review_mode` | `"auto"\|"human"` | yes | `auto` = PR auto-merges to epic when CI passes. `human` = requires approval. |

### Branch / worktree

| Field | Type | Required | Description |
|---|---|---|---|
| `base_branch` | `str` | yes | Branch the worker bases its work on. |
| `worker_branch` | `str` | yes | Branch the worker commits to. |
| `worktree_name` | `str \| null` | no | Git worktree directory name for isolated execution. |

### Scope

| Field | Type | Required | Description |
|---|---|---|---|
| `objective` | `str` | yes | One-paragraph implementation goal. |
| `acceptance_criteria` | `list[str]` | no | Conditions that must be true for "Done". |
| `implementation_constraints` | `list[str]` | no | Hard rules, e.g. `"do not modify app/ui/"`. |
| `allowed_paths` | `list[str]` | no | Glob patterns the worker may write to. `[]` = no restriction. |
| `forbidden_paths` | `list[str]` | no | Glob patterns the worker must never write to. |
| `related_files_hint` | `list[str]` | no | Informational — files likely relevant (not a whitelist). |

`allowed_paths` and `forbidden_paths` must not overlap (validated at construction).

### Checks

| Field | Type | Required | Description |
|---|---|---|---|
| `required_checks` | `list[str]` | no | Shell commands that must pass before the worker marks Done. |
| `optional_checks` | `list[str]` | no | Informational — failures do not block completion. |

Empty strings in either list are rejected at construction.

### Done definition and failure policy

| Field | Type | Required | Description |
|---|---|---|---|
| `done_definition` | `str` | no | Plain-English "Done" description. |
| `failure_policy.on_check_failure` | `"abort"\|"warn"` | default `"abort"` | How the worker handles a failed required check. |
| `failure_policy.max_retries` | `int` 0–5 | default `0` | Retry budget before giving up. |
| `failure_policy.escalate_to_cloud` | `bool` | default `false` | Ask watcher to hand off to a cloud session on failure. |

### State mapping

| Field | Default | Description |
|---|---|---|
| `ticket_state_map.in_progress_local` | `"InProgressLocal"` | State set when worker starts. |
| `ticket_state_map.merged_to_epic` | `"MergedToEpic"` | State set after PR merges to epic. |
| `ticket_state_map.ready_for_review` | `"EpicReadyForCloudReview"` | State set when epic PR is ready. |
| `ticket_state_map.failed` | `"Blocked"` | State set on worker failure. |

### Artifacts

| Field | Type | Required | Description |
|---|---|---|---|
| `artifact_paths.result_json` | `str` | yes | Path the worker writes `{status, summary, checks_passed}` to. |
| `artifact_paths.manifest_copy` | `str` | yes | Copy of the executed manifest for audit. |

Use `ArtifactPaths.from_ticket_id("WOR-77")` to generate defaults.

---

## Example manifest

```json
{
  "manifest_version": "1.0",
  "ticket_id": "WOR-78",
  "epic_id": "WOR-75",
  "title": "Update Linear workflow states and ticket lifecycle",
  "priority": 2,
  "status": "ReadyForLocal",
  "parallel_safe": true,
  "risk_level": "low",
  "risk_flags": [],
  "implementation_mode": "local",
  "review_mode": "auto",
  "base_branch": "wor-75-hybrid-execution-engine",
  "worker_branch": "wor-78-update-linear-workflow-states-and-ticket-lifecycle",
  "worktree_name": "worktree-wor-78",
  "objective": "Add new Linear workflow states to support the hybrid lifecycle and update CLAUDE.md to document the new lifecycle.",
  "acceptance_criteria": [
    "Required Linear states exist: Groomed, ReadyForLocal, InProgressLocal, MergedToEpic, EpicReadyForCloudReview, MainPRReady",
    "CLAUDE.md documents the new lifecycle with a state transition diagram",
    "local-ready label exists in Linear"
  ],
  "implementation_constraints": [
    "Do not modify app/ui/ or app/core/",
    "CLAUDE.md changes only — no Python code"
  ],
  "allowed_paths": [
    "CLAUDE.md",
    ".claude/**"
  ],
  "forbidden_paths": [
    "app/**",
    "tests/**"
  ],
  "related_files_hint": [
    "CLAUDE.md",
    ".claude/commands/start-ticket.md"
  ],
  "required_checks": [
    "pre-commit run --files CLAUDE.md"
  ],
  "optional_checks": [],
  "done_definition": "CLAUDE.md updated with the new state diagram and all required Linear states exist.",
  "failure_policy": {
    "on_check_failure": "abort",
    "max_retries": 0,
    "escalate_to_cloud": false
  },
  "ticket_state_map": {
    "in_progress_local": "InProgressLocal",
    "merged_to_epic": "MergedToEpic",
    "ready_for_review": "EpicReadyForCloudReview",
    "failed": "Blocked"
  },
  "artifact_paths": {
    "result_json": ".claude/artifacts/wor_78/result.json",
    "manifest_copy": ".claude/artifacts/wor_78/manifest.json"
  }
}
```

---

## Consuming the schema in Python

```python
from app.core.manifest import ExecutionManifest, ArtifactPaths

# Producer (cloud) — build and write a manifest
manifest = ExecutionManifest(
    ticket_id="WOR-78",
    title="Update Linear workflow states",
    priority=2,
    status="ReadyForLocal",
    parallel_safe=True,
    risk_level="low",
    implementation_mode="local",
    review_mode="auto",
    base_branch="wor-75-hybrid-execution-engine",
    worker_branch="wor-78-update-linear-workflow-states-and-ticket-lifecycle",
    objective="Add new Linear workflow states and update CLAUDE.md.",
    artifact_paths=ArtifactPaths.from_ticket_id("WOR-78"),
)
manifest.to_json(".claude/manifests/wor-78.json")

# Worker (local) — load and validate
manifest = ExecutionManifest.from_json(".claude/manifests/wor-78.json")
print(manifest.required_checks)
```

---

## JSON Schema for non-Python consumers

The schema is exported at `schemas/execution_manifest.schema.json` and can be used to validate manifests from any language. Regenerate after model changes:

```bash
python -c "
import json
from app.core.manifest import ExecutionManifest
print(json.dumps(ExecutionManifest.json_schema(), indent=2))
" > schemas/execution_manifest.schema.json
```
