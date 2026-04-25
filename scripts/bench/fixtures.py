"""Fixture generation for the benchmark suite."""

from __future__ import annotations

from pathlib import Path

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

_PROJECT_SUMMARY_TEMPLATE = """\
# Project Summary: repo-scaffold-desktop

## Overview

repo-scaffold-desktop is a desktop application for generating repository
scaffolds from configurable presets. It supports Jinja2 templates, optional
git initialization, pre-commit hook installation, CI workflow generation,
and CODEOWNERS configuration. The application is built with PySide6 for the
GUI and exposes a CLI for automation.

## Architecture

The codebase follows a strict layered architecture:

- **app/core/** — all business logic; no UI code allowed here
- **app/ui/** — PySide6 presentation layer; calls core, contains no logic
- **templates/** — Jinja2 template files rendered during scaffold generation
- **tests/** — unit tests for core logic only
- **schemas/** — exported JSON Schemas for non-Python consumers
- **scripts/bench/** — standalone benchmark suite; no app.* imports allowed

Data flows one way: UI → config model → generator → disk.

## Module Responsibilities

- **config.py** — Pydantic input models (repo name, output path, preset)
- **presets.py** — preset definitions (maps preset name → file list)
- **generator.py** — renders templates and writes files to disk
- **post_setup.py** — side effects: git init, pre-commit install, etc.
- **user_prefs.py** — UserPreferences model and PrefsStore (JSON persistence)
- **manifest.py** — ExecutionManifest Pydantic model: cloud→local contract
- **escalation_policy.py** — EscalationPolicy: loads escalation_policy.toml
- **linear_client.py** — thin Linear GraphQL client (stdlib urllib only)
- **metrics.py** — SQLite-backed store for per-ticket cost and metrics
- **bench_store.py** — SQLite store for benchmark run records (GPU, timing)

## Engineering Principles

1. UI stays thin. No branching logic, no file I/O in app/ui/.
2. Prefer config + templates over conditional generation logic.
3. Generated output must be deterministic and easy to diff.
4. Avoid over-abstracting v1. Three similar lines beat a premature helper.
5. Side effects (git, pre-commit) live only in post_setup.py.
6. Architecture contracts are enforced by Import Linter.

## Benchmark Suite

The benchmark suite (scripts/bench/) evaluates local LLM backends:

- **speed** — minimal prompt measuring raw generation latency
- **coding** — coding task with automated quality evaluation
- **prefill_shared** — long shared document prefix to test KV-cache reuse
- **prefill_unshared** — fresh random document each run (cold prefill)
- **boundary** — context-window edge probing

The runner collects GPU metrics (VRAM, utilization, power, temperature,
SM/memory clocks), system metrics (RAM, CPU offload detection), timing
metrics (TTFT, wall time, throughput), and quality metrics for coding tasks.

## Development Workflow

Each ticket follows grooming → planning → local implementation → PR phases.
The watcher daemon polls Linear for ReadyForLocal tickets and orchestrates
local worker sessions in isolated git worktrees.

"""


def generate_fixtures() -> None:
    """Create scripts/bench/fixtures/project_summary_50k.txt (~50k tokens)."""
    _FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    target = _FIXTURES_DIR / "project_summary_50k.txt"
    # Target: ~50k tokens ≈ 200k chars at 4 chars/token
    target_chars = 200_000
    base = _PROJECT_SUMMARY_TEMPLATE
    parts = [base]
    section_idx = 0
    while sum(len(p) for p in parts) < target_chars:
        section_idx += 1
        parts.append(
            f"\n\n## Extended Notes — Section {section_idx}\n\n"
            + base.replace(
                "repo-scaffold-desktop", f"repo-scaffold-desktop (ref {section_idx})"
            )
        )
    content = "".join(parts)[:target_chars]
    target.write_text(content, encoding="utf-8")
    print(f"Fixture written: {target} ({len(content):,} chars)")
