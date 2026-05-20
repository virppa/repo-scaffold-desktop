"""WOR-504 spike sweep driver: 6 cells x (operator-paced vLLM restart) x bench.

For each cell:
  1. Print the full vLLM launch command with the cell's --max-num-batched-tokens value
  2. Prompt operator to restart vLLM, wait for Enter
  3. Probe ``/v1/models`` to confirm vLLM is up
  4. Snapshot ``/metrics`` BEFORE the cell
  5. Invoke ``run_bench.py`` with the cell's config TOML
  6. Snapshot ``/metrics`` AFTER, compute deltas (prefix-cache hit ratio,
     mean TTFT, preemption count -- now reliable post-WOR-439)
  7. Record the sweep_id for the final pair-wise ``--compare`` pass and append
     it to a persistent state file so the sweep is RESUMABLE across sessions.

State file: ``.claude/artifacts/wor_504/sweep_state.json`` is updated after
EVERY successful cell. Re-running the script picks up where it left off --
already-complete cells are skipped silently. Use ``--restart`` to wipe state
and start fresh, or ``--cells X,Y`` to force-re-run a named subset.

After all cells (or when all 6 are recorded): pair-wise
``run_bench.py --compare baseline cell`` against the 4096 baseline.

Phase 0 hook (``--phase-0``): print the canonical 0.95 launch command and
parse the operator's pasted "GPU KV cache size: N tokens" line into a
proposed ``PRODUCTION_KV_CACHE_TOKENS`` update.

Usage:
    python scripts/bench/run_wor504_sweep.py --phase-0
    python scripts/bench/run_wor504_sweep.py             # resume from state file
    python scripts/bench/run_wor504_sweep.py --restart   # wipe state, start over
    python scripts/bench/run_wor504_sweep.py --cells vllm_bt_8192,vllm_bt_16384
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# (backend_id, bench config path, --max-num-batched-tokens value, note)
CELLS: tuple[tuple[str, str, str, str], ...] = (
    ("vllm_bt_4096", "config/bench-bt-4096.toml", "4096", "baseline (current prod)"),
    ("vllm_bt_8192", "config/bench-bt-8192.toml", "8192", "2x current"),
    ("vllm_bt_16384", "config/bench-bt-16384.toml", "16384", "4x current"),
    ("vllm_bt_32768", "config/bench-bt-32768.toml", "32768", "8x current"),
    ("vllm_bt_65536", "config/bench-bt-65536.toml", "65536", "16x current (KV cap)"),
    (
        "vllm_bt_chunkoff",
        "config/bench-bt-chunkoff.toml",
        "131072",
        "chunked prefill OFF at coding tier",
    ),
)

VLLM_BASE_URL = "http://localhost:8000"
PRODUCTION_KV_CACHE_TOKENS = 148_816  # current constant; Phase 0 may revise
STATE_PATH = Path(".claude/artifacts/wor_504/sweep_state.json")


def _load_state() -> dict[str, dict[str, Any]]:
    """Return per-cell state keyed by backend_id. Empty dict if no state yet."""
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    cells = data.get("cells", {})
    return cells if isinstance(cells, dict) else {}


def _save_state(state: dict[str, dict[str, Any]]) -> None:
    """Atomically write the cell state file."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cells": state}, indent=2))
    tmp.replace(STATE_PATH)


def _print_launch_command(bt_value: str) -> None:
    """Print the vLLM launch command for one cell (operator copy-pastes)."""
    print(
        f"""
{"=" * 80}
  Restart vLLM in WSL2 with --max-num-batched-tokens {bt_value}
  (copy-paste -- adjust ONLY the --max-num-batched-tokens value)
{"=" * 80}

/home/antti/vllm-env/bin/vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \\
  --served-model-name qwen3-coder \\
  --max-model-len 262144 --max-num-seqs 16 \\
  --gpu-memory-utilization 0.95 \\
  --kv-cache-dtype fp8 --max-num-batched-tokens {bt_value} \\
  --reasoning-parser qwen3 --enable-prefix-caching \\
  --language-model-only --safetensors-load-strategy prefetch \\
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \\
  --default-chat-template-kwargs '{{"preserve_thinking": true}}'
"""
    )


def _probe_vllm_ready(timeout_total: float = 60.0) -> bool:
    """Probe ``/v1/models`` until 200 or timeout. Returns True if ready."""
    start = time.monotonic()
    while time.monotonic() - start < timeout_total:
        try:
            with urllib.request.urlopen(  # nosec B310
                f"{VLLM_BASE_URL}/v1/models", timeout=3
            ) as resp:
                if resp.status == 200:
                    return True
        except (OSError, Exception):  # noqa: BLE001
            pass
        time.sleep(2)
    return False


def _capture_metrics_snapshot() -> dict[str, float]:
    """Capture key vLLM /metrics counters (post-WOR-439).

    Sums across labeled variants so the snapshot is per-server, not per-label.
    Returns empty dict if /metrics is unavailable (older vLLM or disabled).
    """
    keys = (
        "vllm:prefix_cache_hits",
        "vllm:prefix_cache_queries",
        "vllm:num_preemptions_total",
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "vllm:time_to_first_token_seconds_sum",
        "vllm:time_to_first_token_seconds_count",
    )
    try:
        with urllib.request.urlopen(  # nosec B310
            f"{VLLM_BASE_URL}/metrics", timeout=5
        ) as resp:
            text = resp.read().decode("utf-8")
    except (OSError, Exception):  # noqa: BLE001
        return {}
    snap: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        for k in keys:
            if line.startswith(k + " ") or line.startswith(k + "{"):
                try:
                    val = float(line.rsplit(" ", 1)[1])
                    snap[k] = snap.get(k, 0.0) + val
                except (ValueError, IndexError):
                    pass
                break
    return snap


def _compute_delta(
    before: dict[str, float], after: dict[str, float]
) -> dict[str, float]:
    """Subtract before from after; add derived hit-ratio and mean TTFT."""
    delta: dict[str, float] = {k: v - before.get(k, 0.0) for k, v in after.items()}
    queries = delta.get("vllm:prefix_cache_queries", 0.0)
    hits = delta.get("vllm:prefix_cache_hits", 0.0)
    if queries > 0:
        delta["derived:prefix_cache_hit_ratio"] = hits / queries
    ttft_sum = delta.get("vllm:time_to_first_token_seconds_sum", 0.0)
    ttft_n = delta.get("vllm:time_to_first_token_seconds_count", 0.0)
    if ttft_n > 0:
        delta["derived:ttft_mean_seconds"] = ttft_sum / ttft_n
    return delta


def _run_bench_cell(config_path: str) -> str | None:
    """Invoke run_bench.py; return sweep_id parsed from its stdout."""
    cmd = [sys.executable, "scripts/bench/run_bench.py", "--config", config_path]
    print(f"\n  Running: {' '.join(cmd)}\n")
    proc = subprocess.run(  # nosec B603
        cmd, capture_output=True, text=True, check=False
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return None
    match = re.search(r"Sweep:\s+(run_\d{8}_\d{6})", proc.stdout)
    return match.group(1) if match else None


def _run_phase_0() -> int:
    """Phase 0: prompt for KV pool size at --gpu-memory-utilization 0.95."""
    print(
        """
================================================================================
  PHASE 0 -- Re-measure KV pool at --gpu-memory-utilization 0.95 (post-WOR-527)
================================================================================
"""
    )
    _print_launch_command("4096")
    print(
        """  Watch the vLLM startup log for a line like:

       GPU KV cache size: 148,816 tokens
       (or:  GPU KV cache size: 148816 tokens)

  Paste the line (or just the number) below. Empty line to abort.
"""
    )
    line = input("  KV cache size line: ").strip()
    if not line:
        print("  Aborted.")
        return 1
    m = re.search(r"([\d,]+)\s*tokens", line) or re.search(r"(\d[\d,]*)", line)
    if not m:
        print(f"  No number found in: {line!r}")
        return 1
    new_kv = int(m.group(1).replace(",", ""))
    old_kv = PRODUCTION_KV_CACHE_TOKENS
    delta_pct = 100.0 * (new_kv - old_kv) / old_kv
    print(
        f"""
  Previous (0.90): {PRODUCTION_KV_CACHE_TOKENS:,} tokens
  New      (0.95): {new_kv:,} tokens
  Delta          : {delta_pct:+.2f}%
"""
    )
    if abs(delta_pct) >= 2.0:
        print(
            f"  >=2% change -- UPDATE app/core/watcher/watcher_helpers.py:\n"
            f"      PRODUCTION_KV_CACHE_TOKENS = {new_kv:_}"
        )
    else:
        print("  <2% change -- no constant update needed.")
    return 0


def _format_cell_summary(
    backend_id: str, sweep_id: str | None, delta: dict[str, float]
) -> str:
    cache = delta.get("derived:prefix_cache_hit_ratio")
    ttft = delta.get("derived:ttft_mean_seconds")
    preempts = int(delta.get("vllm:num_preemptions_total", 0))
    cache_s = f"{cache:.3f}" if cache is not None else "-"
    ttft_s = f"{ttft:.3f}s" if ttft is not None else "-"
    return (
        f"  {backend_id:20s} sweep={sweep_id or 'FAILED':18s} "
        f"cache_hit={cache_s:>6} ttft={ttft_s:>7} preempts={preempts}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WOR-504 BT sweep driver")
    parser.add_argument(
        "--phase-0",
        action="store_true",
        help="Phase 0 only: prompt for KV pool measurement at 0.95",
    )
    parser.add_argument(
        "--cells",
        default="all",
        help=(
            "Comma-separated cell names. Default 'all' resumes from state file "
            "(skips cells already complete). Explicit list force-runs those cells."
        ),
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Wipe sweep state file and start over from cell 1",
    )
    args = parser.parse_args(argv)

    if args.phase_0:
        return _run_phase_0()

    if args.restart and STATE_PATH.exists():
        STATE_PATH.unlink()
        print(f"  State wiped: {STATE_PATH}")

    state = _load_state()
    explicit_cells = args.cells != "all"

    selected = (
        list(CELLS)
        if not explicit_cells
        else [c for c in CELLS if c[0] in args.cells.split(",")]
    )
    if not selected:
        print(f"  No matching cells: {args.cells}", file=sys.stderr)
        return 1

    if state:
        done = [b for b in state if state[b].get("sweep_id")]
        if done:
            print(f"\n  Resuming from state ({len(done)}/{len(CELLS)} cells done):")
            for b in done:
                print(f"    [done] {b}  sweep={state[b]['sweep_id']}")
            print(
                "  Use --restart to wipe state and re-run, "
                "or --cells X,Y to force-re-run specific cells."
            )

    for i, (backend_id, cfg_path, bt_value, note) in enumerate(selected, 1):
        # Skip already-done cells unless explicitly named.
        if (
            not explicit_cells
            and backend_id in state
            and state[backend_id].get("sweep_id")
        ):
            print(
                f"\n  Cell {i}/{len(selected)}: {backend_id} "
                f"already complete (sweep={state[backend_id]['sweep_id']}) -- skipping"
            )
            continue

        print(f"\n{'=' * 80}")
        print(f"  Cell {i}/{len(selected)}: {backend_id} ({note})")
        print(f"{'=' * 80}")
        _print_launch_command(bt_value)
        input("  Press Enter when vLLM is restarted and ready... ")

        if not _probe_vllm_ready():
            print(
                "  ERROR: vLLM did not become ready within 60s. Skipping cell.",
                file=sys.stderr,
            )
            continue
        print("  vLLM is up. Capturing pre-cell /metrics snapshot.")
        snap_before = _capture_metrics_snapshot()
        sweep_id = _run_bench_cell(cfg_path)
        snap_after = _capture_metrics_snapshot()
        delta = _compute_delta(snap_before, snap_after)

        # Persist after EVERY cell so the sweep is resumable on next invocation.
        state[backend_id] = {"sweep_id": sweep_id, "metrics_delta": delta}
        _save_state(state)

        print(f"\n  Cell {i} complete: sweep_id={sweep_id}")
        print(f"  State saved to: {STATE_PATH}")
        for line in _format_cell_summary(backend_id, sweep_id, delta).splitlines():
            print(f"    {line.strip()}")

    print(f"\n{'=' * 80}\n  Current sweep state\n{'=' * 80}")
    final_state = _load_state()
    for backend_id, _, _, _ in CELLS:
        entry = final_state.get(backend_id, {})
        print(
            _format_cell_summary(
                backend_id, entry.get("sweep_id"), entry.get("metrics_delta", {})
            )
        )

    completed = {b: e for b, e in final_state.items() if e.get("sweep_id")}
    if len(completed) < len(CELLS):
        print(
            f"\n  {len(completed)}/{len(CELLS)} cells complete. "
            "Re-run the script to continue; --compare runs only after all 6 finish."
        )
        return 0

    baseline_sweep = completed.get("vllm_bt_4096", {}).get("sweep_id")
    if not baseline_sweep:
        print(
            "\n  All cells done, but vllm_bt_4096 baseline missing -- skipping compare."
        )
        return 0

    print(f"\n  Pair-wise --compare vs baseline ({baseline_sweep}):")
    for backend_id, _, _, _ in CELLS:
        if backend_id == "vllm_bt_4096":
            continue
        sweep_id = completed.get(backend_id, {}).get("sweep_id")
        if not sweep_id:
            continue
        print(f"\n  -- {backend_id} vs vllm_bt_4096 --")
        subprocess.run(  # nosec B603
            [
                sys.executable,
                "scripts/bench/run_bench.py",
                "--compare",
                baseline_sweep,
                sweep_id,
            ],
            check=False,
        )

    print(f"\n  Final results in: {STATE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
