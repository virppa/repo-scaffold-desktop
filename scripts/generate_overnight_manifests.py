"""Generate manifests for the WOR-313 mega overnight hardening epic.

Writes 27 ExecutionManifest JSON files to .claude/artifacts/<id>/manifest.json,
validates each via the ExecutionManifest model, and prints a summary.

Run: python scripts/generate_overnight_manifests.py

Data lives in scripts/manifests/wor313.yaml; this script is a thin entry point.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import yaml  # noqa: F401
except ImportError:  # pragma: no cover
    print("PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from app.core.manifest_builder import TaxonomyFields, build_manifest, write_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    yaml_path = REPO_ROOT / "scripts" / "manifests" / "wor313.yaml"
    artifacts_root = REPO_ROOT / ".claude" / "artifacts"

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    tickets = data["tickets"]
    failures: list[str] = []
    successes: list[str] = []

    for t in tickets:
        ticket_id = t["ticket_id"]
        artifact_dir = artifacts_root / ticket_id.lower().replace("-", "_")
        artifact_dir.mkdir(parents=True, exist_ok=True)

        try:
            manifest_dict = build_manifest(
                ticket_id=t["ticket_id"],
                epic_id="WOR-313",
                branch=t["branch"],
                title=t["title"],
                allowed_paths=t["allowed_paths"],
                related_files_hint=t["related_files_hint"],
                effort=t["effort"],
                change_type=t["change_type"],
                taxonomy=TaxonomyFields(
                    reasoning_demand=t["reasoning_demand"],
                    scope_clarity=t["scope_clarity"],
                    constraint_density=t["constraint_density"],
                    ac_specificity=t["ac_specificity"],
                ),
                objective=t["objective"],
                acceptance_criteria=t["acceptance_criteria"],
                implementation_constraints=t["implementation_constraints"],
                tech_stack=t["tech_stack"],
                raw_extensions=t["raw_extensions"],
                forbidden_paths_extra=t.get("forbidden_paths_extra", []),
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{ticket_id}: {exc}")
            continue

        manifest_path = artifact_dir / "manifest.json"
        write_manifest(manifest_path, manifest_dict)
        successes.append(ticket_id)

    print(f"Wrote {len(successes)} manifests successfully:")
    for tid in successes:
        print(f"  {tid}")

    if failures:
        print(f"\n{len(failures)} failures:")
        for f in failures:
            print(f"  {f}")
        return 1

    print(f"\nTotal: {len(tickets)} tickets, {len(successes)} written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
