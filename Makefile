# Repo task shortcuts. Most day-to-day commands live in CLAUDE.md; this file
# holds the few that are awkward to type or need a loop.

FLAKE_RUNS ?= 5

.PHONY: flake-hunt test lint typecheck

## flake-hunt: run the full suite FLAKE_RUNS times under -n 8; fail on the
## first non-green run. The xdist-parallel determinism gate (WOR-506). Run
## this locally after any change to watcher tests or shared test fixtures —
## it is deliberately NOT wired into every CI run (5x suite time would 5x CI
## cost against the cost-economics milestone). CI runs the suite once; this
## is the on-demand flake reproducer.
flake-hunt:
	@i=1; while [ $$i -le $(FLAKE_RUNS) ]; do \
		echo "=== flake-hunt run $$i/$(FLAKE_RUNS) ==="; \
		python -m pytest -q || { echo "FLAKY: run $$i failed"; exit 1; }; \
		i=$$((i+1)); \
	done; \
	echo "flake-hunt: $(FLAKE_RUNS) consecutive green runs under -n 8"

## test: full suite (xdist, as CI runs it minus coverage)
test:
	python -m pytest -q

## lint: ruff lint + format check
lint:
	ruff check . && ruff format --check .

## typecheck: mypy on the app package
typecheck:
	mypy app/
