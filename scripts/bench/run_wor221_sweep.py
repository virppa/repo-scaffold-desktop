"""WOR-221 parameter sweep — per-step bench runner.

Each step corresponds to one vLLM server config. Start vLLM manually in WSL2
with the printed command, then run this script for that step. Results land in
bench.db tagged by backend_id. Steps are fully independent — run them in any
order, on different days, as many times as needed.

Usage:
    python scripts/bench/run_wor221_sweep.py --step A   # baseline
    python scripts/bench/run_wor221_sweep.py --step B   # chunked prefill on, 4096
    python scripts/bench/run_wor221_sweep.py --step C   # chunked prefill on, 8192
    python scripts/bench/run_wor221_sweep.py --step D   # chunked prefill on, 16384
    python scripts/bench/run_wor221_sweep.py --step E   # num_scheduler_steps=4
    python scripts/bench/run_wor221_sweep.py --step F   # num_scheduler_steps=8
    python scripts/bench/run_wor221_sweep.py --step G   # max_num_seqs=8 sanity check
    python scripts/bench/run_wor221_sweep.py --list     # show all steps and commands

Resume an interrupted bench run:
    python scripts/bench/run_wor221_sweep.py --step A --resume run_20260429_XXXXXX
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

# Ensure repo root is on sys.path when run as `python scripts/bench/run_wor221_sweep.py`
sys.path.insert(0, str(Path(__file__).parents[2]))

from scripts.bench.drivers.vllm import VllmDriver  # noqa: E402

CONFIG = "config/bench-wor221.toml"
MODEL = "/home/antti/models/Qwen3.6-35B-A3B-NVFP4"
VLLM_BASE_URL = "http://localhost:8000"

_BASE_FLAGS = [
    "--max-model-len 262144",
    "--kv-cache-dtype fp8",
    "--reasoning-parser qwen3",
    "--enable-prefix-caching",
    "--language-model-only",
    "--safetensors-load-strategy prefetch",
]


def _cmd(*extra: str) -> str:
    return "vllm serve " + MODEL + " " + " ".join(_BASE_FLAGS + list(extra))


STEPS: dict[str, dict] = {
    "A": {
        "backend_id": "vllm_chunk_off_4096",
        "label": "Baseline — no chunked prefill, max_num_batched_tokens=4096",
        "vllm_cmd": _cmd("--max-num-seqs 200", "--max-num-batched-tokens 4096"),
        "note": (
            "Reference point. All other steps are compared against this one.\n"
            "FP8 baselines from bench.db:\n"
            "  coding 131K c=2: ~101 tok/s per-req\n"
            "  boundary 262K c=2: ~90 tok/s per-req"
        ),
    },
    "B": {
        "backend_id": "vllm_chunk_on_4096",
        "label": "Chunked prefill ON, max_num_batched_tokens=4096 (Mamba minimum)",
        "vllm_cmd": _cmd(
            "--max-num-seqs 200",
            "--max-num-batched-tokens 4096",
            "--enable-chunked-prefill",
        ),
        "note": (
            "Smallest chunked-prefill config. Keeps batched_tokens at the Mamba\n"
            "block_size=2096 minimum. If this shows no improvement over A, larger\n"
            "batched_tokens (C, D) won't help either."
        ),
    },
    "C": {
        "backend_id": "vllm_chunk_on_8192",
        "label": "Chunked prefill ON, max_num_batched_tokens=8192",
        "vllm_cmd": _cmd(
            "--max-num-seqs 200",
            "--max-num-batched-tokens 8192",
            "--enable-chunked-prefill",
        ),
        "note": "Larger chunk budget — gives scheduler more prefill tokens per step.",
    },
    "D": {
        "backend_id": "vllm_chunk_on_16384",
        "label": "Chunked prefill ON, max_num_batched_tokens=16384",
        "vllm_cmd": _cmd(
            "--max-num-seqs 200",
            "--max-num-batched-tokens 16384",
            "--enable-chunked-prefill",
        ),
        "note": "Maximum chunk budget tested. Watch for throughput regression vs A.",
    },
    "E": {
        "backend_id": "vllm_sched_steps_4",
        "label": "num_scheduler_steps=4 (baseline otherwise same as A)",
        "vllm_cmd": _cmd(
            "--max-num-seqs 200",
            "--max-num-batched-tokens 4096",
            "--num-scheduler-steps 4",
        ),
        "note": (
            "Reduces CPU-GPU sync overhead by doing 4 forward steps per call.\n"
            "NOTE: --num-scheduler-steps may not exist in vLLM 0.20.0.\n"
            "If the server fails to start, skip E+F and note 'flag unavailable'."
        ),
    },
    "F": {
        "backend_id": "vllm_sched_steps_8",
        "label": "num_scheduler_steps=8",
        "vllm_cmd": _cmd(
            "--max-num-seqs 200",
            "--max-num-batched-tokens 4096",
            "--num-scheduler-steps 8",
        ),
        "note": "Higher step count — likely diminishing returns or regression vs E.",
    },
    "G": {
        "backend_id": "vllm_seqs_8",
        "label": "max_num_seqs=8 — queue pressure sanity check",
        "vllm_cmd": _cmd(
            "--max-num-seqs 8",  # intentionally tight
            "--max-num-batched-tokens 4096",
        ),
        "note": (
            "Forces maximum queue pressure at c=2. If throughput matches A,\n"
            "the 200-seq ceiling is confirmed non-binding for our workload."
        ),
    },
}


def wait_for_vllm(timeout_s: int = 300) -> bool:
    """Poll VllmDriver.is_available() until the server responds or timeout."""
    driver = VllmDriver(base_url=VLLM_BASE_URL)
    print(f"Polling {VLLM_BASE_URL}/v1/models", end="", flush=True)
    for _ in range(timeout_s):
        if driver.is_available():
            print(" ready")
            return True
        time.sleep(1)
        print(".", end="", flush=True)
    print(" TIMEOUT")
    return False


def run_bench(backend_id: str, resume: str | None) -> int:
    cmd = [
        sys.executable,
        "scripts/bench/run_bench.py",
        "--config",
        CONFIG,
        "--backend",
        backend_id,
    ]
    if resume:
        cmd += ["--resume", resume]
    return subprocess.run(cmd, check=False).returncode


def print_step(step: str, info: dict) -> None:
    print(f"\nStep {step} — {info['label']}")
    print("-" * 60)
    if info.get("note"):
        for line in info["note"].splitlines():
            print(f"  {line}")
    print()
    print("Start vLLM in WSL2 with this one-liner:")
    print()
    print(f"  {info['vllm_cmd']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--step",
        choices=list(STEPS),
        metavar="STEP",
        help="Which config to run: A B C D E F G",
    )
    parser.add_argument(
        "--resume",
        metavar="SWEEP_ID",
        help="Resume an interrupted bench run by sweep ID",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print all steps with their vLLM commands and exit",
    )
    args = parser.parse_args()

    if args.list:
        print("WOR-221 steps and vLLM commands\n")
        for step, info in STEPS.items():
            print_step(step, info)
            print()
        return

    if not args.step:
        parser.print_help()
        sys.exit(1)

    info = STEPS[args.step]
    print_step(args.step, info)

    print(f"Waiting for vLLM to be ready at {VLLM_BASE_URL}/v1/models ...")
    if not wait_for_vllm():
        print("vLLM did not respond within 5 minutes. Check WSL2 terminal.")
        sys.exit(1)

    # Let CUDA graphs and JIT finish compiling before the first real request.
    print("Pausing 20s for CUDA graph warm-up...")
    time.sleep(20)

    bid = info["backend_id"]
    print(f"\nRunning bench (backend_id={bid!r})...")
    rc = run_bench(bid, args.resume)

    if rc == 0:
        print(f"\nStep {args.step} complete. Results in bench.db under {bid!r}.")
        print("View: python scripts/bench/run_bench.py --browse")
    else:
        print(f"\nBench exited with code {rc}. Partial results may be in bench.db.")
    sys.exit(rc)


if __name__ == "__main__":
    main()
