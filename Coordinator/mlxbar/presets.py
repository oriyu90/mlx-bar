"""Bulk setting presets: light / full / custom (DESIGN_v2.6.0.md section 1).

The preset footprint is exactly six keys (memory, disk, KV quantization);
concurrency counts stay with the setup wizard and the user. All values are
derived from live hardware at apply time, clamped into `SettingsStore`
validation ranges, so applying a preset can never produce an invalid config.

`effective_mode()` re-derives both profiles from current hardware and
compares them against the live settings: any manual tweak of a managed key
automatically shows as "custom" without storing anything.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import __version__

# Preset-managed keys, in dotted form for export/import reporting.
MANAGED_KEYS = (
    "experimental.kvCacheQuantization.mode",
    "models.pool.totalMemoryRatio",
    "models.pool.minimumSystemReserveGB",
    "models.pool.defaultPerModelMaxGB",
    "promptCache.diskMaxGB",
    "promptCache.diskWriteBudgetGB",
)

MODES = ("light", "full")


def _clamp(value: float, minimum: float, maximum: float) -> int | float:
    return max(minimum, min(maximum, value))


def compute_profile(mode: str, physical_gb: float, free_gb: float) -> dict[str, Any]:
    """Hardware-derived values for `mode`. Always within _validate ranges."""
    if mode not in MODES:
        raise ValueError("mode must be light or full")
    light = mode == "light"
    return {
        "experimental.kvCacheQuantization.mode": "8bit" if light else "bf16",
        "models.pool.totalMemoryRatio": 0.80 if light else 0.85,
        "models.pool.minimumSystemReserveGB": int(_clamp(
            round(physical_gb * (0.15 if light else 0.10)), 4, 32)),
        "models.pool.defaultPerModelMaxGB": int(_clamp(
            round(physical_gb * (0.25 if light else 0.35)), 4 if light else 8,
            64 if light else 128)),
        "promptCache.diskMaxGB": int(_clamp(
            round(free_gb * (0.02 if light else 0.04)), 2 if light else 4,
            20 if light else 40)),
        "promptCache.diskWriteBudgetGB": int(_clamp(
            round(free_gb * (0.05 if light else 0.10)), 8 if light else 16,
            64 if light else 128)),
    }


def profile_warnings(free_gb: float) -> list[str]:
    """Advisories that travel with an apply response; never block it."""
    warnings: list[str] = []
    if free_gb <= 0:
        warnings.append("ディスク空き容量を検出できなかったため最小値で適用しました")
    elif free_gb < 50:
        warnings.append("ディスク空き容量が少ないためキャッシュ上限は最小寄りで適用しました")
    warnings.append("KVキャッシュ量子化の変更はサービス再起動後に反映されます")
    return warnings


def _get_dotted(settings: dict, dotted: str) -> Any:
    node: Any = settings
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def effective_mode(settings: dict, physical_gb: float, free_gb: float) -> str:
    """Which preset the live settings match right now (recomputed)."""
    for mode in MODES:
        profile = compute_profile(mode, physical_gb, free_gb)
        if all(_get_dotted(settings, key) == value for key, value in profile.items()):
            return mode
    return "custom"


def to_patch(profile: dict[str, Any]) -> dict[str, Any]:
    """Expand a flat profile into the nested PUT/patch shape."""
    patch: dict[str, Any] = {}
    for dotted, value in profile.items():
        node = patch
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return patch


def status_payload(settings: dict, hardware: dict[str, Any]) -> dict[str, Any]:
    """Response body for GET /presets/status (also reused by the CLI)."""
    physical_gb = float((hardware.get("memory") or {}).get("physicalGB", 0) or 0)
    free_gb = float((hardware.get("disk") or {}).get("freeGB", 0) or 0)
    presets = settings.get("presets", {}) or {}
    return {
        "stored": presets.get("mode", "custom"),
        "effective": effective_mode(settings, physical_gb, free_gb),
        "managedKeys": list(MANAGED_KEYS),
        "light": compute_profile("light", physical_gb, free_gb),
        "full": compute_profile("full", physical_gb, free_gb),
        "lastApplied": presets.get("lastApplied"),
        "warnings": profile_warnings(free_gb),
    }


def apply_payload(mode: str, hardware: dict[str, Any]) -> tuple[dict, dict, list]:
    """(settings patch, lastApplied record, warnings) for POST /presets/apply."""
    physical_gb = float((hardware.get("memory") or {}).get("physicalGB", 0) or 0)
    free_gb = float((hardware.get("disk") or {}).get("freeGB", 0) or 0)
    profile = compute_profile(mode, physical_gb, free_gb)
    patch = to_patch(profile)
    record = {"mode": mode, "at": datetime.now(timezone.utc).isoformat(),
              "appVersion": __version__, "values": profile}
    patch.setdefault("presets", {})["mode"] = mode
    patch["presets"]["lastApplied"] = record
    return patch, record, profile_warnings(free_gb)
