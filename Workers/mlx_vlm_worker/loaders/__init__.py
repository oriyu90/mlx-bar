"""Bundled loaders for non-standard MLX artifacts (v3.2.0 / ISS-5).

Scope and safety (option B of the ISS-5 design):

* Only code that ships **in this repository** is ever executed. A model
  directory's own ``*.py`` (e.g. ``niwaki_flash_load.py``) is never imported,
  scanned or executed -- a model download is not a trusted code source
  (AC-5-4). Option A (run the model's loader) stays unimplemented by default.
* Loaders are selected from evidence in ``config.json`` / the directory
  layout, never by filename matching alone.
* Every loader runs a **preflight** before any weights are touched, so an
  artifact that cannot work fails in milliseconds instead of allocating tens
  of gigabytes first.
* Enabling bundled loaders is an explicit opt-in (``models.bundledLoaders``
  in the coordinator settings, latched into ``MLXBAR_BUNDLED_LOADERS=1`` for
  the worker process) and each use is logged.
"""
from __future__ import annotations

import json
from pathlib import Path

from common.model_errors import ModelLoadError

from .base import BundledLoader, LoaderSpec
from .niwaki_qwen4_exp import NiwakiQwen4ExpLoader

# Adding a loader means adding it here: the registry is closed on purpose so a
# discovered directory cannot name a loader that does not exist in this build.
LOADERS: dict[str, BundledLoader] = {
    loader.spec.loader_id: loader
    for loader in (NiwakiQwen4ExpLoader(),)
}

TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "tokenizer.model")
SUPPORTED_QUANT_BITS = frozenset({2, 3, 4, 5, 6, 8})


def available_loaders() -> list[dict]:
    """Describe the bundled loaders (read-only; used by diagnostics/tests)."""
    return [{"loaderId": loader.spec.loader_id,
             "displayName": loader.spec.display_name,
             "description": loader.spec.description,
             "modelTypes": list(loader.spec.model_types),
             "implemented": loader.spec.implemented}
            for loader in LOADERS.values()]


def resolve(loader_id: str | None) -> BundledLoader | None:
    if not loader_id:
        return None
    return LOADERS.get(str(loader_id))


def read_config(path: str | Path) -> dict:
    config_path = Path(path) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ModelLoadError(
            "MODEL_FORMAT_UNSUPPORTED",
            "config.json を読み取れないため、このモデルの形式を判定できません",
            detail=f"{config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ModelLoadError(
            "MODEL_FORMAT_UNSUPPORTED",
            "config.json がオブジェクトではないため、このモデルの形式を判定できません",
            detail=str(config_path))
    return config


def detect_loader(config: dict, path: str | Path | None = None) -> BundledLoader | None:
    """The bundled loader this artifact needs, if any (config evidence only)."""
    for loader in LOADERS.values():
        try:
            if loader.matches(config, path):
                return loader
        except Exception:  # noqa: BLE001 - a matcher must never break a load
            continue
    return None


def _weight_files(path: Path) -> list[Path]:
    return sorted(path.glob("*.safetensors"))


def _runtime_modules() -> list:
    """Importable runtime model packages (``mlx_vlm.models`` / ``mlx_lm.models``)."""
    import importlib

    modules = []
    for name in ("mlx_vlm.models", "mlx_lm.models"):
        try:
            modules.append(importlib.import_module(name))
        except Exception:  # noqa: BLE001 - absence is "unknown", not "unsupported"
            continue
    return modules


def runtime_implements(model_type: str) -> bool | None:
    """Whether an installed runtime has an implementation for `model_type`.

    True/False when at least one runtime was importable, None when no runtime
    could be inspected (the callers then keep the historical behaviour instead
    of refusing a model they cannot judge).
    """
    if not model_type:
        return None
    key = model_type.casefold().replace("-", "_")
    inspected = False
    for module in _runtime_modules():
        directory = getattr(module, "__file__", None)
        if not directory:
            continue
        package = Path(directory).parent
        try:
            names = {entry.name.casefold() for entry in package.iterdir() if entry.is_dir()}
        except OSError:
            continue
        inspected = True
        if key in names or key.replace("_", "") in {name.replace("_", "") for name in names}:
            return True
    return False if inspected else None


def generic_preflight(path: str | Path) -> dict:
    """Check an artifact *before* any load attempt (ISS-5 / AC-5-2).

    Raises :class:`ModelLoadError` only for conditions that are certain from
    the files on disk (missing tokenizer, missing/empty weights, an
    unsupported quantization width, an architecture no installed runtime
    implements). Anything merely unknown passes through: refusing a model we
    cannot judge would be a regression.
    """
    path = Path(path)
    config = read_config(path)
    report = {"path": str(path), "checks": []}

    tokenizer_present = any((path / name).exists() for name in TOKENIZER_FILES)
    report["checks"].append({"name": "tokenizer", "ok": tokenizer_present})
    if not tokenizer_present:
        raise ModelLoadError(
            "MODEL_TOKENIZER_MISSING",
            "tokenizer ファイルが見つかりません（tokenizer.json / tokenizer_config.json / "
            "tokenizer.model のいずれかが必要です）",
            detail=str(path))

    weights = _weight_files(path)
    index_path = path / "model.safetensors.index.json"
    index_ok = True
    missing_files: list[str] = []
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            weight_map = index.get("weight_map") if isinstance(index, dict) else None
            if not isinstance(weight_map, dict) or not weight_map:
                index_ok = False
            else:
                for name in {str(value) for value in weight_map.values()}:
                    if not (path / name).exists():
                        missing_files.append(name)
        except Exception:
            index_ok = False
    report["checks"].append({"name": "weights", "ok": bool(weights) and index_ok
                             and not missing_files,
                             "files": len(weights),
                             "missing": missing_files[:5]})
    if not weights:
        raise ModelLoadError(
            "MODEL_FORMAT_UNSUPPORTED",
            "重みファイル（*.safetensors）が見つかりません。モデルのダウンロードが不完全です",
            detail=str(path))
    if not index_ok:
        raise ModelLoadError(
            "MODEL_FORMAT_UNSUPPORTED",
            "model.safetensors.index.json を読み取れません。ダウンロードが不完全か破損しています",
            detail=str(index_path))
    if missing_files:
        raise ModelLoadError(
            "MODEL_FORMAT_UNSUPPORTED",
            "index が参照する重みファイルが不足しています"
            f"（{', '.join(missing_files[:3])} ほか）",
            detail=str(path))

    quantization = config.get("quantization")
    if isinstance(quantization, dict) and "bits" in quantization:
        bits = quantization.get("bits")
        if isinstance(bits, int) and not isinstance(bits, bool) and bits not in SUPPORTED_QUANT_BITS:
            raise ModelLoadError(
                "MODEL_QUANT_UNSUPPORTED",
                f"量子化ビット数 {bits} は未対応です（対応: "
                f"{', '.join(str(value) for value in sorted(SUPPORTED_QUANT_BITS))} bit）",
                detail=json.dumps(quantization)[:500])
        report["checks"].append({"name": "quantization", "ok": True, "bits": bits})
    else:
        report["checks"].append({"name": "quantization", "ok": True, "bits": None})

    model_type = config.get("model_type")
    implemented = runtime_implements(str(model_type) if model_type else "")
    report["checks"].append({"name": "architecture", "ok": implemented is not False,
                             "modelType": model_type,
                             "implemented": implemented})
    if implemented is False:
        raise ModelLoadError(
            "MODEL_FORMAT_UNSUPPORTED",
            f"アーキテクチャ '{model_type}' はインストール済みのランタイムに実装がありません",
            detail=str(model_type))
    return report


def plan(path: str | Path, *, bundled_loaders_enabled: bool) -> dict:
    """Decide how `path` would be loaded, failing fast when it cannot be.

    Returns ``{"loader", "loaderId", "preflight", "config"}``. Raises
    :class:`ModelLoadError` with the actionable code:
    ``MODEL_TOKENIZER_MISSING`` / ``MODEL_FORMAT_UNSUPPORTED`` /
    ``MODEL_QUANT_UNSUPPORTED`` / ``MODEL_NEEDS_BUNDLED_LOADER``.
    """
    path = Path(path)
    report = generic_preflight(path)
    config = read_config(path)
    loader = detect_loader(config, path)
    if loader is None:
        return {"loader": None, "loaderId": None, "preflight": report, "config": config}
    loader_report = loader.preflight(path)
    report["loader"] = loader_report
    if not bundled_loaders_enabled:
        raise ModelLoadError(
            "MODEL_NEEDS_BUNDLED_LOADER",
            f"このモデルは同梱ローダ「{loader.spec.display_name}」が必要です"
            "（既定では無効です。設定 models.bundledLoaders を有効にすると使用できます）",
            detail=loader.spec.description)
    if not loader.spec.implemented:
        raise ModelLoadError(
            "MODEL_NEEDS_BUNDLED_LOADER",
            f"同梱ローダ「{loader.spec.display_name}」は検出と事前検証のみ実装されています。"
            "重みの再構成（kept_experts / PLE）は未実装のため、このモデルはまだロードできません",
            detail=loader.spec.description)
    return {"loader": loader, "loaderId": loader.spec.loader_id,
            "preflight": report, "config": config}


__all__ = [
    "BundledLoader", "LoaderSpec", "LOADERS", "available_loaders", "resolve",
    "read_config", "detect_loader", "generic_preflight", "plan",
    "runtime_implements", "SUPPORTED_QUANT_BITS", "TOKENIZER_FILES",
]
