# Claude project guide

This repo is a Python desktop tool that generates opinionated starter repositories for agent-driven development.

## What we are building

The app should:
- collect repo setup options from the user
- generate folders and files from presets/templates
- optionally initialize git and run basic post-setup commands
- stay simple, testable, and modular

## Main architecture

- `app/core/` contains the real business logic
- `app/ui/` contains the PySide6 desktop layer
- `templates/` contains scaffold templates and presets
- `tests/` contains tests for generator logic

## Design rules

- Keep UI thin
- Put logic in reusable core modules
- Prefer config + templates over complex branching
- Write small, testable functions
- Avoid overengineering
- Keep generated output deterministic

## Priorities

1. make generator logic work
2. make presets clean
3. add minimal UI
4. add optional git/post-setup actions
5. improve presets and developer experience

## Guardrails

- Use Ruff formatting/linting conventions already in repo
- Add or update tests when changing generator behavior
- Keep README and docs aligned with current behavior
- Do not introduce large dependencies without a clear need
- Prefer incremental changes over broad rewrites

## Workflow

- Work from a Linear issue when possible
- Use issue key in branch names, PR titles, and commits
- Keep PRs small and focused
- Treat CI and formatting as required, not optional

## When helping

Prefer:
- concrete file edits
- minimal working implementations
- explicit tradeoff notes
- clear next steps

Avoid:
- unnecessary abstraction
- speculative features
- rewriting unrelated files