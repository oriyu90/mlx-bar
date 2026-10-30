"""Hardware inventory for presets, setup, and export (DESIGN_v2.6.0.md 4).

Single source of truth for "what machine is this". Every probe is
individually guarded: `collect()` never raises, it records failures in
`warnings` instead, so opening Settings or running a scan can never crash
the coordinator or the GUI over an unreadable sysctl.

Deliberately dependency-free (stdlib only, no psutil) and worker-free, using
the same macOS commands the pool admission path already relies on
(`vm_stat`, `sysctl kern.memorystatus_vm_pressure_level`).
"""
from __future__ import annotations

import contextlib
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

GIB = 1024 ** 3


def physical_memory_bytes() -> int:
    """Total RAM in bytes, 0 when undetectable."""
    with contextlib.suppress(AttributeError, OSError, ValueError):
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    return 0


# v3.2.3 (WP-005): how much of the inactive/speculative population may be
# counted as reclaimable at each macOS pressure level. At NORMAL the kernel
# will hand those pages back cheaply; once the OS reports WARN or worse, a
# large share of them is already dirty and reclaiming them costs a swap
# storm, so only half is counted. This keeps available a monotone
# non-increasing function of pressure (AC-MS-3).
PRESSURE_RECLAIM_DIVISOR = {0: 1, 1: 2, 2: 2, 4: 2}


def host_memory() -> dict[str, int | bool]:
    """Reclaimable bytes plus the macOS VM pressure level (1/2/4).

    Mirrors ModelPoolSupervisor._host_capacity's commands so both layers
    agree on what "free" means; kept separate so this module stays
    importable without the worker stack.

    v3.2.3 (WP-005): ``measured`` is additive and says whether both probes
    actually produced a value. Callers that guard memory safety must treat
    ``measured is False`` as "unknown" and fail closed, instead of reading
    ``availableBytes == 0`` as "the machine is full" (or ignoring the guard).
    """
    pressure = 0
    pressure_measured = False
    try:
        output = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True, text=True, timeout=5).stdout
        pressure = int(output.strip())
        pressure_measured = pressure >= 0
    except (OSError, ValueError, subprocess.SubprocessError):
        pressure = 0

    available = 0
    pages_measured = False
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        output = subprocess.run(["/usr/bin/vm_stat"], capture_output=True,
                                text=True, timeout=5).stdout
        free = inactive = speculative = purgeable = 0
        for line in output.splitlines():
            head, _, tail = line.rpartition(":")
            try:
                pages = int(tail.strip().rstrip("."))
            except ValueError:
                continue
            if head.startswith("Pages free"):
                free += pages
            elif head.startswith("Pages inactive"):
                inactive += pages
            elif head.startswith("Pages speculative"):
                speculative += pages
            elif head.startswith("Pages purgeable"):
                purgeable += pages
        if free or inactive or speculative or purgeable:
            pages_measured = True
            # Counted after summing so the result never depends on which page
            # class happened to be odd.
            reclaimable_pages = inactive + speculative
            divisor = PRESSURE_RECLAIM_DIVISOR.get(pressure, 1)
            available = (free + purgeable + reclaimable_pages // divisor) * page_size
    except (OSError, ValueError, subprocess.SubprocessError):
        available = 0
    return {"availableBytes": available, "pressureLevel": pressure,
            "measured": pages_measured and pressure_measured}


def cpu_info() -> dict[str, Any]:
    """Brand string and logical core count; empty brand when undetectable."""
    brand = ""
    try:
        output = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=5).stdout
        brand = output.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    cores = 0
    try:
        cores = int(os.cpu_count() or 0)
    except (OSError, ValueError):
        pass
    return {"brand": brand, "logicalCores": cores, "machine": platform.machine()}


def disk_info(path: str | Path) -> dict[str, Any]:
    """Total/free bytes of the volume holding `path`; zeros when unreadable."""
    total = free = 0
    resolved = ""
    try:
        resolved = str(Path(path).expanduser().resolve())
        usage = shutil.disk_usage(resolved)
        total, free = int(usage.total), int(usage.free)
    except OSError:
        pass
    return {"path": resolved, "totalBytes": total, "freeBytes": free}


def lm_studio_default_folder() -> dict[str, Any]:
    """The model folder the setup wizard offers first (scanner's fallback)."""
    folder = str(Path.home() / ".lmstudio" / "models")
    return {"defaultFolder": folder, "exists": Path(folder).is_dir()}


def default_max_resident_models(physical_gb: float) -> int:
    """Shared default for the simultaneous-models question (DESIGN 3.3)."""
    if physical_gb <= 16:
        return 1
    if physical_gb <= 64:
        return 2
    return 3


def collect(data_dir: str | Path | None = None) -> dict[str, Any]:
    """Full inventory. Never raises; problems land in `warnings`."""
    warnings: list[str] = []
    physical = physical_memory_bytes()
    if physical <= 0:
        warnings.append("物理メモリ容量を検出できませんでした")
    memory = host_memory()
    cpu = cpu_info()
    if not cpu.get("brand"):
        warnings.append("CPU名を検出できませんでした")
    try:
        from .settings import app_support_dir
        anchor = data_dir or app_support_dir()
    except Exception:  # noqa: BLE001 - anchor fallback must not fail
        anchor = Path.home()
    disk = disk_info(anchor)
    if disk["freeBytes"] <= 0:
        warnings.append("ディスク空き容量を検出できませんでした")
    physical_gb = physical / GIB if physical > 0 else 0.0
    return {
        "memory": {"physicalBytes": physical,
                   "physicalGB": round(physical_gb, 1),
                   "availableBytes": memory["availableBytes"],
                   "pressureLevel": memory["pressureLevel"]},
        "cpu": cpu,
        "disk": {**disk,
                 "totalGB": round(disk["totalBytes"] / GIB, 1),
                 "freeGB": round(disk["freeBytes"] / GIB, 1)},
        "lmStudio": lm_studio_default_folder(),
        "defaultMaxResidentModels": default_max_resident_models(physical_gb),
        "warnings": warnings,
    }
