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

    @classmethod
    def capture(cls, backend: str, model: str, config: BenchConfig) -> "EnvSnapshot":
        gpu_driver, cuda_ver = _get_nvidia_info()
        return cls(
            backend=backend,
            model=model,
            gpu_driver_version=gpu_driver,
            cuda_version=cuda_ver,
            python_version=sys.version,
            os_version=platform.platform(),
            settings_hash=_hash_settings(config.model_dump()),
        )


def _hash_settings(settings: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def _get_nvidia_info() -> tuple[str | None, str | None]:
    """Return (driver_version, cuda_version) or (None, None) if nvidia-smi absent."""
    try:
        driver_result = subprocess.run(  # nosec B603, B607
            [
                "nvidia-smi",
                "--query-gpu=driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("nvidia-smi unavailable: %s", exc)
        return None, None

    if driver_result.returncode != 0:
        return None, None

    lines = driver_result.stdout.strip().splitlines()
    driver_version: str | None = lines[0].strip() if lines else None

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
                    parts = line.split("CUDA Version:")
                    if len(parts) > 1:
                        cuda_version = parts[1].split("|")[0].strip()
                    break
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    return driver_version or None, cuda_version
