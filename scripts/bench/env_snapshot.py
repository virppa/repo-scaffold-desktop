"""One-shot environment snapshot per benchmark sweep."""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from typing import Any

from .config import BenchConfig

logger = logging.getLogger(__name__)


@dataclass
class EnvSnapshot:
    backend: str
    model: str
    gpu_driver_version: str | None
    cuda_version: str | None
    python_version: str
    os_version: str
    settings_hash: str
    total_vram_gb: float | None = None

    @classmethod
    def capture(cls, backend: str, model: str, config: BenchConfig) -> "EnvSnapshot":
        gpu_driver, cuda_ver, total_vram_gb = _get_nvidia_info()
        return cls(
            backend=backend,
            model=model,
            gpu_driver_version=gpu_driver,
            cuda_version=cuda_ver,
            python_version=sys.version,
            os_version=platform.platform(),
            settings_hash=_hash_settings(config.model_dump()),
            total_vram_gb=total_vram_gb,
        )


def _hash_settings(settings: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def _get_nvidia_info() -> tuple[str | None, str | None, float | None]:
    """Return (driver_version, cuda_version, total_vram_gb) or (None, None, None)."""
    try:
        # Query driver_version and memory.total in one CSV call (one row per GPU)
        csv_result = subprocess.run(  # nosec B603, B607
            [
                "nvidia-smi",
                "--query-gpu=driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("nvidia-smi unavailable: %s", exc)
        return None, None, None

    if csv_result.returncode != 0:
        return None, None, None

    driver_version: str | None = None
    total_mib = 0
    for line in csv_result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if driver_version is None and parts[0]:
            driver_version = parts[0]
        if len(parts) >= 2:
            try:
                total_mib += int(parts[1])
            except ValueError:
                pass

    total_vram_gb: float | None = total_mib / 1024 if total_mib > 0 else None

    cuda_version: str | None = None
    try:
        smi_result = subprocess.run(  # nosec B603, B607
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if smi_result.returncode == 0:
            for line in smi_result.stdout.splitlines():
                if "CUDA Version:" in line:
                    parts_cuda = line.split("CUDA Version:")
                    if len(parts_cuda) > 1:
                        cuda_version = parts_cuda[1].split("|")[0].strip()
                    break
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return driver_version or None, cuda_version, total_vram_gb
