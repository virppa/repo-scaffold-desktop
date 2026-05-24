# Spike: Qwen3.6-35B-A3B MTP vs Concurrent Workers

**Question:** Can a single MTP-enabled vLLM worker beat the current 2-worker concurrent
aggregate on **effective ticket throughput** (wall-clock per merged sub-ticket), enough
to justify deleting the concurrent-dispatch orchestration code?

**TL;DR:** Unknown — gated on one measurement (current solo GPU duty cycle).
If duty cycle is ≥80%, MTP-solo cannot win and this spike ends at Phase 1.
If duty cycle is ≤60%, MTP-solo is in striking distance and Phase 2 runs.

---

## Background

- Production today: vLLM 0.20.0 serving `Qwen3.6-35B-A3B-NVFP4`, watcher dispatches up
  to 2 concurrent local workers (WOR-410). Measured aggregate: ~190 tok/s at 2 workers
  vs ~27 tok/s solo. The 7× ratio is duty-cycle, not raw GPU — concurrent workers fill
  each other's tool-call idle gaps.
- vLLM exposes MTP speculative decoding for Qwen3.6-35B-A3B via
  `--speculative-config '{"method":"mtp","num_speculative_tokens":2}'`. Requires the
  MTP-head checkpoint (`model_mtp.safetensors`) bundled with the trunk.
- Published vLLM MTP wins on this model family are ~1.27× decode (Vassallo, RTX 3090).
  llama.cpp/GGUF MTP wins are higher (1.5–2× per Unsloth card) but **do not transfer**
  to our path — switching to GGUF loses Blackwell NVFP4 (its own >2× cliff on the 5090).
- Known incompatibility: vLLM issue
  [#38182](https://github.com/vllm-project/vllm/issues/38182) reports MTP **reduces
  prefix-cache hit rate**. Production currently runs `--enable-prefix-caching`.

## Why the question matters

If MTP-solo wins, the simplification dividend is substantial:

| Code path deleted by going solo | Today's owner |
|---|---|
| Epic-branch overlap gate (WOR-419) | `app/core/watcher/watcher.py` |
| `__init__.py` overlap carve-out (WOR-410) | `app/core/watcher/watcher_helpers.py` |
| File-conflict detection at `/start-epic` | `.claude/commands/start-epic.md` |
| Per-worker git-worktree lifecycle | `app/core/watcher/watcher_worktrees.py` |
| `--max-local-workers` pool plumbing | `app/core/watcher/watcher.py` |
| Multi-dispatch-per-cycle saturation logic | `app/core/watcher/watcher.py` |

The "primary fast worker + opportunistic filler" hybrid was considered and
**rejected**: it reintroduces every line above plus a new "is primary stalled?"
scheduler. Pick one — pure solo or pure pool. The hybrid is the worst of both.

## Decision rule

```
effective_output = decode_speed × gpu_active_fraction

mtp_solo_wins ⇔
  (decode_speed_mtp × active_fraction_solo)
    > (decode_speed_concurrent_aggregate × active_fraction_concurrent)
```

Pure tok/s comparisons are the wrong metric. Wall-clock per merged sub-ticket on
a real ticket trace is the only number that decides this.

---

## Phase 1 — Duty-cycle gate (CHEAP, run first)

**Goal:** Measure `gpu_active_fraction` on a real solo-worker ticket under the
current vLLM-NVFP4 config (no MTP, no flag changes). One number ranks the whole
experiment.

### Instrumentation

Add lightweight wall-clock accounting to the worker loop in
`app/core/watcher/watcher_subprocess.py`. Two timestamps per tool round-trip:

- `t_decode_end` — moment the assistant message completes (last token of the
  assistant turn emitted by vLLM).
- `t_tool_result_received` — moment the tool result is appended to the
  conversation and the next API call is sent.

Sum across the session:

- `T_generating` = sum of (assistant-turn end - assistant-turn start)
- `T_tool_idle` = sum of (next-turn start - assistant-turn end)
- `gpu_active_fraction = T_generating / (T_generating + T_tool_idle)`

Persist to `ticket_metrics` as a new nullable column `gpu_active_fraction` via
`metrics.py` `_migrate()` (ADD COLUMN, no schema breakage).

### Sample size

Run on 5 representative tickets covering the workload spread:

1. A "facade-split" ticket (heavy edits, low test churn)
2. A test-only ticket (high pytest fan-out)
3. A documentation ticket (mostly Read + Edit)
4. A bug-fix ticket with `required_checks` re-runs
5. A refactor ticket with many parallel-Reads (WOR-387 pattern)

Report median + IQR. Single-ticket measurements are useless — the variance is real.

### Gate

| `gpu_active_fraction` (median) | Decision |
|---|---|
| ≥ 80% | **Stop.** MTP-solo cannot beat current concurrent aggregate. No headroom to recover. Close spike, recommend keeping concurrent dispatch. |
| 60–80% | **Marginal.** Run Phase 2 with low expectations; the simplicity case has to be very strong to overcome the throughput gap. |
| ≤ 60% | **Strong signal.** Run Phase 2. MTP-solo is in striking distance and the orchestration-deletion dividend is worth chasing. |

---

## Phase 2 — Three-mode bench (gated on Phase 1)

**Run only if Phase 1 says "marginal" or "strong signal".**

### Modes

| Mode | vLLM config delta | Watcher config |
|---|---|---|
| A. Baseline (current) | unchanged | `--max-local-workers 2` |
| B. MTP-solo, prefix-cache on | `--speculative-config '{"method":"mtp","num_speculative_tokens":1}'` | `--max-local-workers 1` |
| C. MTP-solo, prefix-cache off | B + `--no-enable-prefix-caching` | `--max-local-workers 1` |

`num_speculative_tokens=1` per Vassallo's published optimum on this model
on consumer hardware; if Phase 1 shows very long generating phases, also test
`num_speculative_tokens=2`.

### Workload

Re-run the same 5 tickets from Phase 1 in each mode. The tickets must be
**replayed**, not freshly assigned — use Linear's `Backlog` + a captured manifest
to ensure identical scope across modes. Worker prompt non-determinism is real;
run each (mode, ticket) cell 3 times and report median.

### Metrics

Per-cell:

- **Wall-clock minutes** (start of `/implement-ticket` → result artifact written)
- **GPU active fraction** (Phase 1 instrumentation)
- **Tokens generated** (`ticket_metrics.total_output_tokens`)
- **Tool-call round-trip count**
- **`required_checks` retry count**
- **MTP acceptance rate** (Modes B, C only — vLLM logs this)
- **Final state** (`MergedToEpic`, `Blocked`, `Escalated`)

### Winner criterion

Lowest median wall-clock per `MergedToEpic` ticket, with no regression on
final-state distribution. **Pure tok/s is decorative — do not rank on it.**

---

## Phase 3 — Production rollout (gated on Phase 2)

Run only if Phase 2 picks Mode B or C as winner.

1. Update `CLAUDE.md` § "Local model development" with the new serve command.
2. Delete the code paths listed in the "Why the question matters" table.
3. Reduce `--max-local-workers` default from 8 to 1 in
   `app/core/watcher/watcher.py`.
4. Repurpose freed VRAM/seq budget: lower `--max-num-seqs` from 16 to ~4 (one
   live + headroom for MTP verify pass + retry-launch overlap).
5. File follow-up tickets to delete dead orchestration code surface-by-surface.

## Out of scope

- llama.cpp / GGUF MTP path. Tested and rejected at planning time (loses NVFP4).
- Qwen3-Next family. Different architecture, different MTP method name
  (`qwen3_next_mtp`). Separate spike if we ever migrate model families.
- Multi-GPU. Single 5090, single node.

## Risks

| Risk | Mitigation |
|---|---|
| MTP head not present in current `Qwen3.6-35B-A3B-NVFP4` checkpoint | Verify `ls model_mtp.safetensors` in model dir before Phase 2; if absent, download `unsloth/Qwen3.6-35B-A3B-NVFP4` or equivalent that bundles it. |
| Tool-call parser regression under MTP | Phase 2 includes tool-heavy tickets; final-state regression check catches it. |
| Phase 1 measurement bias from outlier tickets | Median + IQR across 5 ticket archetypes; reject if IQR is too wide to gate on. |
| Concurrent-aggregate number drifts during measurement window | Take the baseline read in the same week as Phase 1; pin watcher version. |

## Exit criteria

Spike closes with one of:

1. **Phase 1 gate fails** → recommendation: keep concurrent dispatch. No code changes.
2. **Phase 2 picks Mode A (baseline)** → recommendation: keep concurrent dispatch. No code changes.
3. **Phase 2 picks Mode B or C** → file Phase-3 implementation tickets, update CLAUDE.md.

## References

- vLLM Recipes — Qwen3.5 & Qwen3.6 usage guide
- Vassallo blog — Speculative decoding with Qwen 3.6-35B-A3B (2026-05-15)
- vLLM issue #38182 — MTP reduces prefix-cache hit rate
- WOR-410 — concurrent-dispatch performance memo (`memory/`)
- WOR-419 — epic-branch overlap gate
- WOR-387 — parallel tool-call efficiency rules
- `docs/spikes/wor-344-vllm-native-anthropic-api.md` — current serving path
- `docs/spikes/vllm-benchmark-plan.md` — bench framework to reuse
