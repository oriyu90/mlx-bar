"""Settings export / import (DESIGN_v2.6.0.md section 2).

Export writes everything the app needs to reproduce a setup (settings plus
version metadata, never secrets). Import applies leaf-by-leaf through the
same `_validate` every PUT goes through: anything this machine cannot honor
fails closed into `skipped` with a reason while the rest still applies.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .settings import SettingsStore, deep_merge

EXPORT_FORMAT = "mlxbar-settings-export"
EXPORT_FORMAT_VERSION = 1
# `api.host` and `security.allowLan` validate only as a pair (see apply_import).
LAN_PAIR_KEYS = ("api.host", "security.allowLan")


def flatten(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Settings tree to sorted dotted leaves. Lists stay whole leaves."""
    if isinstance(node, dict):
        leaves: list[tuple[str, Any]] = []
        for key in sorted(node):
            leaves.extend(flatten(node[key], f"{prefix}{key}." if prefix else f"{key}."))
        return leaves
    return [(prefix.rstrip("."), deepcopy(node))]


def _nested(dotted: str, value: Any) -> dict:
    result: dict = {}
    node = result
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = deepcopy(value)
    return result


def build_export(settings: dict, hardware: dict[str, Any]) -> dict[str, Any]:
    """Envelope for `config export` and the GUI export button."""
    memory = hardware.get("memory") or {}
    cpu = hardware.get("cpu") or {}
    return {
        "format": EXPORT_FORMAT,
        "formatVersion": EXPORT_FORMAT_VERSION,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "appVersion": __version__,
        "schemaVersion": settings.get("schemaVersion", 1),
        "hardware": {"physicalMemoryGB": memory.get("physicalGB", 0),
                     "cpuBrand": cpu.get("brand", ""),
                     "cpuCores": cpu.get("logicalCores", 0)},
        # Secrets (API/LM Studio/RAG tokens) are intentionally excluded:
        # they live outside config.json and must be re-entered on the
        # destination machine.
        "settings": deepcopy(settings),
    }


def _filter_machine_specific(dotted: str, value: Any) -> tuple[Any, str | None, bool]:
    """Drop values meaningless on this machine.

    Returns (value, note, blocked): `blocked` leaves are skipped outright,
    while a non-blocking `note` still applies the filtered value and is
    reported so the user knows something was left out.
    """
    if dotted == "models.roots" and isinstance(value, list):
        if not value:
            return value, None, False
        kept = [entry for entry in value
                if isinstance(entry, str) and Path(entry).expanduser().is_dir()]
        dropped = len(value) - len(kept)
        if not kept:
            return None, "フォルダがこのPCに存在しないため適用しませんでした", True
        if dropped:
            return kept, f"{dropped}件のフォルダがこのPCに存在しないため除外して適用しました", False
        return value, None, False
    if dotted == "models.lmStudio.folder" and value is not None:
        if not isinstance(value, str) or not Path(value).expanduser().is_dir():
            return None, "フォルダがこのPCに存在しないため適用しませんでした", True
    return value, None, False


def apply_import(store: SettingsStore, export: dict,
                 port_available: Callable[[int], bool] | None = None) -> dict[str, Any]:
    """Apply an export envelope. Returns {applied, skipped, importedFrom}.

    Raises ValueError for envelopes this app version cannot understand at
    all (wrong format, newer schema): those fail as a whole with 422.
    """
    if not isinstance(export, dict) or export.get("format") != EXPORT_FORMAT:
        raise ValueError("設定ファイルの形式が不正です")
    if export.get("formatVersion") != EXPORT_FORMAT_VERSION:
        raise ValueError("設定ファイルの版数に対応していません")
    schema = export.get("schemaVersion", 1)
    if not isinstance(schema, int) or schema > 1:
        raise ValueError("新しい版のアプリで書き出された設定ファイルです")
    incoming = export.get("settings")
    if not isinstance(incoming, dict):
        raise ValueError("設定ファイルに設定が含まれていません")

    pending = deepcopy(store.data)
    applied: list[str] = []
    skipped: list[dict[str, str]] = []
    incoming_leaves = {dotted: value for dotted, value in flatten(incoming)}
    processed: set[str] = set()
    for dotted, value in flatten(incoming):
        if dotted == "schemaVersion":
            continue
        # `api.host` and `security.allowLan` are rejected by `_validate` when
        # they do not change together. Applying them one leaf at a time in
        # alphabetical order could therefore never import a LAN setup at all:
        # `api.host` alone failed validation, then `security.allowLan` alone
        # did too, and both were reported as skipped (v2.6.0 regression untest-
        # ed until v2.8.1). When the export carries both, validate them as one
        # candidate cluster.
        if dotted in LAN_PAIR_KEYS and dotted not in processed:
            partner = LAN_PAIR_KEYS[0] if dotted == LAN_PAIR_KEYS[1] else LAN_PAIR_KEYS[1]
            if partner in incoming_leaves:
                processed.update({dotted, partner})
                candidate = deep_merge(pending, _nested(dotted, value))
                candidate = deep_merge(candidate, _nested(partner, incoming_leaves[partner]))
                try:
                    SettingsStore._validate(candidate)
                except ValueError as exc:
                    skipped.append({"key": dotted, "reason": str(exc)})
                    skipped.append({"key": partner, "reason": str(exc)})
                    continue
                pending = candidate
                applied.extend([dotted, partner])
                continue
        filtered, note, blocked = _filter_machine_specific(dotted, value)
        if blocked:
            skipped.append({"key": dotted, "reason": note or "適用できませんでした"})
            continue
        if note is not None:
            value = filtered
        if dotted == "api.port" and port_available is not None and isinstance(value, int):
            try:
                if value != store.data.get("api", {}).get("port") and not port_available(value):
                    skipped.append({"key": dotted, "reason": "ポートがこのPCで使用中のため適用しませんでした"})
                    continue
            except Exception:  # noqa: BLE001 - a failed probe skips, never aborts
                skipped.append({"key": dotted, "reason": "ポートの空きを確認できなかったため適用しませんでした"})
                continue
        candidate = deep_merge(pending, _nested(dotted, value))
        try:
            SettingsStore._validate(candidate)
        except ValueError as exc:
            skipped.append({"key": dotted, "reason": str(exc)})
            continue
        pending = candidate
        applied.append(dotted)
        if note is not None:
            skipped.append({"key": dotted, "reason": note})

    store.update(pending)
    return {"applied": applied, "skipped": skipped,
            "importedFrom": {"appVersion": export.get("appVersion"),
                             "exportedAt": export.get("exportedAt"),
                             "schemaVersion": schema}}
