"""Background RAM monitor: wmic/PowerShell on Windows, /proc/meminfo on Linux."""

from __future__ import annotations

import logging
import platform
import subprocess  # nosec B404
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_OFFLOAD_THRESHOLD_GB = 2.0


@dataclass
class SysResult:
    cpu_offload_detected: bool
    peak_ram_gb: float | None = None


def _read_ram_gb_windows_wmic() -> float | None:
    """Return RAM usage in GB via wmic, or None on any failure (silent)."""
    try:
        result = subprocess.run(  # nosec B603, B607
            [
                "wmic",
                "OS",
                "get",
                "TotalVisibleMemorySize,FreePhysicalMemory",
                "/format:value",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    total_kb: float | None = None
    free_kb: float | None = None
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("FreePhysicalMemory="):
            try:
                free_kb = float(line.split("=", 1)[1])
            except ValueError:
                pass
        elif line.startswith("TotalVisibleMemorySize="):
            try:
                total_kb = float(line.split("=", 1)[1])
            except ValueError:
                pass
    if total_kb is None or free_kb is None:
        return None
    return (total_kb - free_kb) / (1024.0 * 1024.0)


def _read_ram_gb_windows_powershell() -> float | None:
    """Return RAM usage in GB via Get-CimInstance (Windows 11+), or None on failure."""
    try:
        result = subprocess.run(  # nosec B603, B607
            [
                "powershell",
                "-NonInteractive",
                "-NoProfile",
                "-Command",
                "$o=Get-CimInstance Win32_OperatingSystem;"
                '"$($o.FreePhysicalMemory) $($o.TotalVisibleMemorySize)"',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    if len(parts) != 2:
        return None
    try:
        free_kb, total_kb = float(parts[0]), float(parts[1])
        return (total_kb - free_kb) / (1024.0 * 1024.0)
    except ValueError:
        return None


# Cached reader selected on first call: avoids re-probing every poll cycle.
_windows_ram_fn: Callable[[], float | None] | None = None


def _read_ram_gb_windows() -> float | None:
    """Auto-detect the fastest available RAM reader on Windows and cache it."""
    global _windows_ram_fn
    if _windows_ram_fn is not None:
        return _windows_ram_fn()

    val = _read_ram_gb_windows_wmic()
    if val is not None:
        _windows_ram_fn = _read_ram_gb_windows_wmic
        return val

    val = _read_ram_gb_windows_powershell()
    if val is not None:
        _windows_ram_fn = _read_ram_gb_windows_powershell
        return val

    logger.warning(
        "RAM monitoring unavailable on this system (wmic and PowerShell both failed)"
    )
    _windows_ram_fn = lambda: None  # noqa: E731
    return None


def _read_ram_gb_linux() -> float | None:
    """Return current RAM usage in GB via /proc/meminfo, or None if unavailable."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            info: dict[str, float] = {}
            for raw_line in f:
                parts = raw_line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        info[key] = float(parts[1])
                    except ValueError:
                        pass
    except OSError:
        return None
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    if total is None or available is None:
        return None
    return (total - available) / (1024.0 * 1024.0)


def _read_ram_gb() -> float | None:
    """Platform-dispatched RAM usage in GB."""
    if platform.system() == "Windows":
        return _read_ram_gb_windows()
    return _read_ram_gb_linux()


class SysMonitor:
    """Daemon thread that polls RAM every *interval* seconds and detects CPU offload."""

    def __init__(self, interval: float = 1.0) -> None:
        self._interval = interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._baseline_ram_gb: float | None = None
        self._peak_ram_gb: float | None = None
        self._cpu_offload_detected = False

    def _poll_ram_gb(self) -> float | None:
        return _read_ram_gb()

    def _run(self) -> None:
        self._baseline_ram_gb = self._poll_ram_gb()
        while not self._stop_event.is_set():
            ram = self._poll_ram_gb()
            if ram is not None:
                if self._peak_ram_gb is None or ram > self._peak_ram_gb:
                    self._peak_ram_gb = ram
                if (
                    self._baseline_ram_gb is not None
                    and ram - self._baseline_ram_gb > _OFFLOAD_THRESHOLD_GB
                ):
                    self._cpu_offload_detected = True
            self._stop_event.wait(self._interval)

    def start(self) -> None:
        self._stop_event.clear()
        self._baseline_ram_gb = None
        self._peak_ram_gb = None
        self._cpu_offload_detected = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> SysResult:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        return SysResult(
            cpu_offload_detected=self._cpu_offload_detected,
            peak_ram_gb=self._peak_ram_gb,
        )
