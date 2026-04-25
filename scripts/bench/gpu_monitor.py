"""Background GPU monitor: polls nvidia-smi every 0.5 s."""

from __future__ import annotations

import logging
import subprocess  # nosec B404
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_QUERY = (
    "memory.used,utilization.gpu,utilization.memory,power.draw,"
    "temperature.gpu,clocks.sm,clocks.mem"
)
_NVIDIA_SMI_CMD: list[str] = [
    "nvidia-smi",
    f"--query-gpu={_QUERY}",
    "--format=csv,noheader,nounits",
]


@dataclass
class GpuSample:
    peak_vram_gb: float | None
    avg_gpu_util_pct: float | None
    avg_gpu_mem_util_pct: float | None
    avg_power_w: float | None
    peak_temp_c: float | None
    avg_sm_clock_mhz: float | None
    avg_mem_clock_mhz: float | None


def _all_none() -> GpuSample:
    return GpuSample(
        peak_vram_gb=None,
        avg_gpu_util_pct=None,
        avg_gpu_mem_util_pct=None,
        avg_power_w=None,
        peak_temp_c=None,
        avg_sm_clock_mhz=None,
        avg_mem_clock_mhz=None,
    )


def _parse_nvidia_smi(
    stdout: str,
) -> tuple[float, float, float, float, float, float, float] | None:
    """Parse first line of nvidia-smi csv output; return None on any error."""
    lines = stdout.strip().splitlines()
    if not lines:
        return None
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) != 7:
        return None
    try:
        vram_mib, gpu_util, mem_util, power, temp, sm_clk, mem_clk = (
            float(p) for p in parts
        )
    except ValueError:
        return None
    return vram_mib / 1024.0, gpu_util, mem_util, power, temp, sm_clk, mem_clk


class GpuMonitor:
    """Daemon thread that polls nvidia-smi every *interval* seconds."""

    def __init__(self, interval: float = 0.5) -> None:
        self._interval = interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._samples: list[tuple[float, float, float, float, float, float, float]] = []

    def _poll_once(
        self,
    ) -> tuple[float, float, float, float, float, float, float] | None:
        try:
            result = subprocess.run(  # nosec B603, B607
                _NVIDIA_SMI_CMD,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("nvidia-smi unavailable: %s", exc)
            return None
        if result.returncode != 0:
            return None
        return _parse_nvidia_smi(result.stdout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            sample = self._poll_once()
            if sample is not None:
                self._samples.append(sample)
            self._stop_event.wait(self._interval)

    def start(self) -> None:
        self._stop_event.clear()
        self._samples.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> GpuSample:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if not self._samples:
            return _all_none()
        vram = [s[0] for s in self._samples]
        gpu_util = [s[1] for s in self._samples]
        mem_util = [s[2] for s in self._samples]
        power = [s[3] for s in self._samples]
        temp = [s[4] for s in self._samples]
        sm_clk = [s[5] for s in self._samples]
        mem_clk = [s[6] for s in self._samples]
        n = len(self._samples)
        return GpuSample(
            peak_vram_gb=max(vram),
            avg_gpu_util_pct=sum(gpu_util) / n,
            avg_gpu_mem_util_pct=sum(mem_util) / n,
            avg_power_w=sum(power) / n,
            peak_temp_c=max(temp),
            avg_sm_clock_mhz=sum(sm_clk) / n,
            avg_mem_clock_mhz=sum(mem_clk) / n,
        )
