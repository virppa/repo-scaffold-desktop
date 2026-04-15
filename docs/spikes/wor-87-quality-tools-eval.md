# WOR-87 Quality Tools Evaluation: Semgrep, deptry, Vulture, Radon

**Date:** 2026-04-15
**Branch:** `wor-87-spike-evaluate-semgrep-deptry-vulture-radon-for-agentic`
**Codebase at time of eval:** `wor-75-hybrid-execution-engine` tip (12 Python files, ~700 LOC)

---

## Summary verdict

| Tool           | Verdict    | Placement                        |
|----------------|------------|----------------------------------|
| Semgrep        | **ADOPT**  | Local hook + subticket CI        |
| deptry         | **ADOPT**  | Subticket CI                     |
| detect-secrets | **ADOPT**  | Local hook (pre-commit)          |
| SonarCloud     | **ADOPT**  | Epic → main PR gate              |
| Vulture        | **REJECT** | N/A — too noisy on Pydantic code |
| Radon          | **DEFER**  | Wait for SonarCloud at epic CI   |

---

## 1. Semgrep

### Setup

```bash
pip install semgrep
# Windows requires UTF-8 env var — add to hook invocation:
PYTHONUTF8=1 semgrep scan --config auto app/ tests/
```

### Findings (2 — both false positives)

Both findings in `app/core/generator.py`:

1. **`python.flask.security.xss.audit.direct-use-of-jinja2`** — flags direct Jinja2 Environment construction. Rule assumes Flask context; inapplicable to a text-file generator. The code already has `# nosec B701`.
2. **`python.lang.security.audit.jinja2.autoescape-disabled`** — flags missing `autoescape=True`. Correct for HTML rendering; not relevant for scaffold file generation (`.py`, `.yaml`, `.md` output).

Both findings are confirmed false positives. Zero true positives on this codebase with `--config auto`.

### Performance

| Config           | Rules | Files | Time  |
|------------------|-------|-------|-------|
| `--config auto`  | 1059  | 12    | ~4 s  |
| `--config p/python` | ~300 | 12 | ~3 s  |

Fast enough for local pre-commit.

### False positive rate

2/2 findings are false positives = 100% FP rate with default config. Resolved by suppressing the two rules in `.semgrepignore` or with inline `# nosec`-style comments (`# nosec` is bandit syntax; Semgrep uses `# nosemgrep`).

### Recommendation: ADOPT

Semgrep provides broader rule coverage than bandit (1059 community rules vs. bandit's single-pass AST checks). Keep bandit in the local hook for now — the two tools have overlapping but not identical coverage. Re-evaluate bandit removal after a full sprint with Semgrep active.

**Required suppressions** (add to `.semgrepignore` or inline):
- `python.flask.security.xss.audit.direct-use-of-jinja2`
- `python.lang.security.audit.jinja2.autoescape-disabled`

**Windows note:** `PYTHONUTF8=1` must be set before invoking Semgrep; the rule download contains characters outside cp1252. This needs to be baked into the pre-commit hook `env:` block.

**Follow-up ticket needed:** Semgrep integration has enough surface area (hook config, suppression file, CI step, Windows compat) to warrant its own sub-ticket.

---

## 2. deptry

### Setup

```bash
pip install deptry
deptry . --requirements-files requirements.txt,requirements-dev.txt
```

### Findings (5 — all real)

All five findings are `DEP003` (transitive dependency imported directly):

| File | Package |
|------|---------|
| `app/cli.py` | `pydantic` |
| `app/core/config.py` | `pydantic` |
| `app/core/generator.py` | `jinja2` |
| `app/core/manifest.py` | `pydantic` |
| `app/core/user_prefs.py` | `pydantic` |

### Root cause

`pyproject.toml` has no `[project.dependencies]` section. deptry treats the project as having no declared direct dependencies, so packages imported in source but absent from `pyproject.toml` are flagged as transitive even though they appear in `requirements.txt`.

`pydantic` and `jinja2` are already in `requirements.txt` — they are correctly declared as direct dependencies there. The fix is to mirror them into `[project.dependencies]` in `pyproject.toml`. This is a real gap: the project currently has split dependency declaration (runtime deps in `requirements.txt`, dev deps in `requirements-dev.txt`) but no pyproject.toml declaration.

### Performance

~instant (< 1 s for 12 files)

### False positive rate

0/5 findings are false positives. All findings point to a real project structure issue.

### Recommendation: ADOPT for CI (not local hook)

deptry is low-friction and zero-config once pyproject.toml is fixed. It should run in subticket CI rather than the local hook because:
- The fix requires pyproject.toml changes (one-time)
- It is not a correctness gate for a single file edit
- Run time is fast but the value is project-wide, not per-file

**Prerequisite:** Add direct runtime dependencies to `pyproject.toml`:

```toml
[project.dependencies]
pydantic = ">=2.0"
jinja2 = ">=3.0"
pyyaml = ">=6.0"
pyside6 = ">=6.0"
```

This also makes the project pip-installable without a separate requirements file.

---

## 3. Vulture

### Setup

```bash
pip install vulture
vulture app/ --min-confidence 60
```

### Findings (55 — nearly all false positives)

55 findings flagged at 60% confidence threshold. Breakdown:

- **Pydantic model fields** (`config.py`, `manifest.py`, `user_prefs.py`): Vulture flags every `field: type = ...` class attribute as "unused variable" because it cannot see that Pydantic accesses these via `__fields__`/`model_fields` at runtime. ~45 of the 55 findings fall into this category.
- **Pydantic validators** (`@validator`/`@field_validator` methods): flagged as "unused method" for the same reason.
- **`manifest.py` public API** (`to_json`, `from_json`, `json_schema`): legitimate public API, not dead code.
- **`VALID_PRESETS`** in `config.py`: potentially a real finding — worth checking if it is actually used elsewhere.

### False positive rate

~50/55 findings (≥90%) are false positives caused by Pydantic's dynamic field access pattern. Vulture has no Pydantic plugin and cannot be configured to understand it without a whitelist that would essentially re-declare every model field.

### Performance

0.111 s — extremely fast, but irrelevant given the signal-to-noise ratio.

### Recommendation: REJECT

Vulture is not viable on a Pydantic-heavy codebase without a comprehensive whitelist. Maintaining a whitelist that mirrors all model fields defeats the purpose. If dead-code detection becomes a priority later, reconsider once the codebase has more plain-Python modules or if a Pydantic-aware Vulture config becomes available.

---

## 4. Radon

### Setup

```bash
pip install radon
radon cc app/ -s -a   # cyclomatic complexity
radon mi app/ -s      # maintainability index
```

### Findings

**Cyclomatic Complexity (CC):**

| Module | Block | Grade | CC |
|--------|-------|-------|----|
| `cli.py` | `main` | C | 11 |
| `cli.py` | `_run_config` | B | 8 |
| `core/generator.py` | `generate` | B | 6 |
| all others | — | A | ≤5 |

Average: **A (2.9)**

Only `cli.main` reaches C (11), which is expected for a CLI dispatcher function; it does not warrant refactoring at this stage.

**Maintainability Index (MI):**

All 12 modules scored **A**, ranging from 47.77 (`user_prefs.py`) to 100 (`main.py`, `post_setup.py`). No module is below the MI "B" threshold (20).

### False positive rate

N/A — Radon is a metric reporter, not a linter. All scores are accurate.

### Performance

< 1 s

### Recommendation: DEFER — wait for SonarQube

The codebase is small and clean. SonarQube is already planned at the epic → main gate and will surface complexity metrics with richer context (cognitive complexity, duplication, test coverage overlay). Adding Radon as a parallel gate would be redundant. Re-evaluate if SonarQube is deprioritised or the codebase grows significantly.

---

---

## 5. SonarCloud (planned integration)

SonarCloud is not one of the four tools evaluated in this spike, but it is already referenced in CLAUDE.md as the intended quality gate at the epic → main boundary. This section records the integration plan so a follow-up ticket can be scoped accurately.

### What SonarCloud provides

- **Static analysis** — broader than bandit/Semgrep for maintainability issues (cognitive complexity, duplication, code smells)
- **Security hotspot review** — triage workflow on top of raw security findings
- **Coverage overlay** — merges pytest-cov data with source to show uncovered lines in the PR diff view
- **Quality gate** — configurable pass/fail threshold (coverage %, new issues, duplications) that can block merging
- **Dashboard** — persistent project health view across PRs and branches

### Where it fits in the branch topology

```
local hook  →  subticket CI  →  epic CI  →  epic→main PR gate
ruff/bandit     semgrep/deptry   SonarCloud   SonarCloud quality gate
```

SonarCloud is intentionally not in the local hook or subticket CI:
- It requires a SonarCloud token and network call — not suitable for fast local loops
- Its value is project-wide metrics and trend data, not per-file correctness
- The quality gate (blocking merge) belongs at the epic → main boundary per CLAUDE.md

### Current state

`sonar-project.properties` exists but is empty. No workflow step exists. A SonarCloud account for the `virppa/repo-scaffold-desktop` repository needs to be activated (free for public repos).

### Required changes

**`sonar-project.properties`:**

```properties
sonar.projectKey=virppa_repo-scaffold-desktop
sonar.organization=virppa
sonar.sources=app
sonar.tests=tests
sonar.python.coverage.reportPaths=coverage.xml
sonar.python.version=3.12
```

**GitHub secret:** `SONAR_TOKEN` — generated from SonarCloud project settings → Analysis Method → GitHub Actions.

**New workflow** `.github/workflows/sonarcloud.yml` (triggers on epic → main PRs only):

```yaml
name: SonarCloud

on:
  pull_request:
    branches: [main]

jobs:
  sonarcloud:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # full history for blame/new-code detection

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements-dev.txt

      - name: Run tests with coverage
        run: pytest --cov=app --cov-report=xml

      - name: SonarCloud scan
        uses: SonarSource/sonarcloud-github-action@master
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

**Alternatively**, add a `sonarcloud` job to the existing `ci.yml` with a branch filter (`if: github.base_ref == 'main'`) to avoid a second workflow file.

### Relationship with Semgrep and bandit

SonarCloud does not replace Semgrep or bandit. The three layers are complementary:

| Tool | Gate | Primary role |
|------|------|--------------|
| bandit | local hook | Fast security AST scan, immediate feedback |
| Semgrep | subticket CI | Broader community rules, blocks PR to epic branch |
| SonarCloud | epic→main PR | Quality gate with coverage, duplication, trend data |

### Quality gate recommendation

Start with SonarCloud's default "Sonar way" gate and tighten after one sprint of data:
- Coverage on new code ≥ 80% (matches existing pytest threshold)
- No new blocker or critical issues
- Duplicated lines on new code < 3%

### Open questions

- Does the SonarCloud free tier cover private repos? (Yes for public; `virppa/repo-scaffold-desktop` is public on GitHub, so free.)
- Should the quality gate block auto-merge of sub-ticket → epic PRs, or only epic → main? Recommendation: epic → main only; sub-ticket merges are already gated by pytest + Semgrep.

---

## 6. detect-secrets (env var / credential protection)

### Context

This tool was added to the spike after the initial four evaluations. The trigger: as the scaffold gains CI workflows and cloud-execution features (manifest, worker handshake), the risk of accidentally committing a real API key or token into a template or config file grows. Pre-commit hooks already block bad Python — this adds a parallel layer for credential hygiene.

### Tool evaluated

**`detect-secrets`** (Yelp, v1.5.0) — scans staged files for 30 secret pattern types (AWS keys, GitHub tokens, high-entropy strings, JWT tokens, private keys, Stripe/Slack/Twilio keys, etc.) before commit. Ships as a pre-commit hook. Maintains a `.secrets.baseline` file to track known false positives so they are not re-flagged.

Alternatives considered and not evaluated:
- **gitleaks** — fast Rust binary, strong git-history scanning. Better for auditing existing history; overkill for a pre-commit-only gate on a clean repo.
- **trufflehog** — deep history scan with verified/unverified classification. Same: historical audit tool, not a commit gate.

### Setup

```bash
pip install detect-secrets
detect-secrets scan . > .secrets.baseline   # one-time: commit this file
```

Pre-commit hook in `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/Yelp/detect-secrets
  rev: v1.5.0
  hooks:
    - id: detect-secrets
      args: ["--baseline", ".secrets.baseline"]
```

### Findings (0 — clean)

```json
"results": {}
```

Zero findings across all 12 Python files, templates, and config files. The codebase contains no hardcoded secrets, high-entropy strings, or credential patterns. This is the expected result and confirms the baseline can be committed as-is.

### Performance

~2.3 s for the full repo — acceptable for a pre-commit hook.

### False positive rate

0/0 — no findings to evaluate. In practice, the most common false positive source is test fixtures containing dummy tokens or base64-encoded test data. Those are handled by adding them to `.secrets.baseline` via `detect-secrets audit .secrets.baseline` (interactive triage). The baseline file keeps false-positive suppression explicit and reviewable in git history.

### Recommendation: ADOPT — local pre-commit hook

detect-secrets belongs at the commit gate alongside bandit. It covers a different threat surface (credential leakage vs. code vulnerabilities) with no overlap. The baseline workflow means false positives are a one-time friction cost, not recurring noise.

**Required files to commit:**
- `.secrets.baseline` — generated by `detect-secrets scan .`; commit alongside the hook config

**Pre-commit placement** (in `.pre-commit-config.yaml`, after bandit/Semgrep):

```yaml
- repo: https://github.com/Yelp/detect-secrets
  rev: v1.5.0
  hooks:
    - id: detect-secrets
      args: ["--baseline", ".secrets.baseline"]
```

**Note on `.env` files:** detect-secrets does not replace `.gitignore` / `.env` hygiene — it catches secrets that _slip through_ into tracked files. The repo already gitignores `.env`; this tool is the backstop if a developer accidentally adds a secret elsewhere (e.g. hardcoded in a test fixture or config value).

**Follow-up ticket needed:** Add `.secrets.baseline`, hook config entry, and CI audit step (run `detect-secrets scan --baseline .secrets.baseline` to catch baseline drift). Can fold into the Semgrep integration ticket.

---

## Decisions and follow-up tickets

| Action | Ticket |
|--------|--------|
| Add Semgrep to pre-commit + CI with suppression config | Create follow-up |
| Add detect-secrets baseline + hook config (can fold into Semgrep ticket) | Create follow-up or fold |
| Fix `pyproject.toml` `[project.dependencies]` + add deptry to CI | Can fold into Semgrep ticket or standalone |
| Activate SonarCloud, populate `sonar-project.properties`, add workflow | Create follow-up |
| Vulture — no action | — |
| Radon — defer until SonarCloud decision | — |
