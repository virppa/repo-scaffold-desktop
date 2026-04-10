# Repo Scaffold Desktop

A Python desktop tool for generating opinionated repository starter kits for agent-driven development.

## Goal

This project creates a configurable base repository setup for solo developers and small teams. It is designed to generate practical scaffolds with options such as:

- pre-commit hooks
- GitHub Actions CI
- PR and issue templates
- CODEOWNERS
- Dependabot
- SonarQube config
- Claude Code project files
- starter repo structure and presets

The first target is a Python desktop application with a simple UI for choosing options and generating a ready-to-use repository locally.

## Project direction

This project is intentionally split into:

- **core generator**: turns config into files and folders
- **desktop UI**: PySide6 interface for choosing options
- **post-setup actions**: optional git init, pre-commit install, and GitHub repo creation
- **template library**: reusable presets for different repo types

## Initial scope

Version 1 should support:

- entering repository name
- choosing a preset
- toggling common quality/devops files
- generating the repository locally
- optional local git initialization

## Planned presets

- Minimal Python repo
- Python desktop app
- Full agentic repo
- Strict repo with more guardrails

## Planned integrations

- GitHub
- Claude Code
- Linear

## Development principles

- Keep the generator logic simple and testable
- Prefer templates and config over hardcoded branching
- Keep UI thin and core logic reusable
- Build CLI-compatible core even if desktop UI is the main entry point
- Add guardrails early: formatting, linting, tests, PR workflow

## Suggested stack

- Python
- PySide6
- Pydantic
- Jinja2
- PyYAML
- pytest
- Ruff
- pre-commit

## Running locally

Create a virtual environment and install dependencies:

```bash
python -m venv .venv