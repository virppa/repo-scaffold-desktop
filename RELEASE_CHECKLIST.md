# Release Checklist — v1

Use this checklist as the gate for calling repo-scaffold-desktop v1 releasable.

---

## Functional readiness

- [ ] App launches successfully in local development environment
- [ ] User can enter repo name and output location
- [ ] User can select a preset
- [ ] User can toggle included scaffold options
- [ ] App generates a local repository successfully
- [ ] Generated output contains expected folders and files
- [ ] Optional git init works
- [ ] Optional pre-commit install works or fails gracefully with useful feedback

## Preset readiness

- [ ] Minimal Python preset works
- [ ] Python desktop preset works
- [ ] Full agentic preset works
- [ ] Presets generate deterministic output
- [ ] No broken placeholders remain in generated files

## Quality readiness

- [ ] Tests for core generator pass
- [ ] Basic failure cases are handled
- [ ] Ruff / formatting checks pass
- [ ] CI passes on default branch
- [ ] No major known blocker bugs remain

## Documentation readiness

- [ ] README explains purpose, setup, and basic usage
- [ ] CLAUDE.md reflects current architecture and workflow
- [ ] Developer can understand how presets/templates are organized
- [ ] Known limitations are documented honestly

## Manual validation

- [ ] Run scaffold generation for each preset
- [ ] Open generated repo and inspect structure
- [ ] Run pre-commit in generated repo where applicable
- [ ] Confirm generated README / CLAUDE / CI files are sensible
- [ ] Confirm output is understandable and editable by a normal developer

## Release decision

- [ ] MVP success criteria from project brief are met
- [ ] Remaining issues are minor and non-blocking
- [ ] First version is useful enough to save real setup time
- [ ] Release notes / summary of current capabilities is prepared
