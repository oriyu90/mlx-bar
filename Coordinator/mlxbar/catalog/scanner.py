from __future__ import annotations

import hashlib
import asyncio
import os
import unicodedata
from pathlib import Path

from .classifier import classify
from .lmstudio import api_models, cli_models


SOURCE_PRIORITY = {"custom_folder": 10, "huggingface_cache": 20, "lm_studio_folder": 30}


def normalized_id(source: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{source}:{digest}"


def canonical_model_path(value: str | Path) -> str:
    path = Path(value).expanduser().resolve()
    if path.is_file() or path.suffix.lower() == ".gguf":
        path = path.parent
    return os.path.normcase(str(path))


def normalized_model_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def add_local_model(models: dict[str, dict], paths: dict[str, str], item: dict) -> None:
    path_key = canonical_model_path(item["path"])
    previous_id = paths.get(path_key)
    if previous_id:
        previous = models[previous_id]
        if SOURCE_PRIORITY.get(item["source"], 0) <= SOURCE_PRIORITY.get(previous["source"], 0):
            return
        models.pop(previous_id, None)
    models[item["id"]] = item
    paths[path_key] = item["id"]


def candidate_directories(root: Path):
    seen: set[tuple[int, int]] = set()
    for current, dirs, files in os.walk(root, followlinks=False):
        try:
            stat = os.stat(current)
        except OSError:
            dirs[:] = []
            continue
        inode = (stat.st_dev, stat.st_ino)
        if inode in seen:
            dirs[:] = []
            continue
        seen.add(inode)
        names = set(files)
        if "config.json" in names or any(name.endswith(".gguf") for name in names):
            yield Path(current)
            dirs[:] = []


def scan_root(root: Path, source: str) -> list[dict]:
    if not root.exists() or not root.is_dir():
        return []
    result = []
    for path in candidate_directories(root):
        real = path.resolve()
        info = classify(real)
        try:
            size = sum(p.stat().st_size for p in real.iterdir() if p.is_file())
        except OSError:
            size = 0
        result.append({
            "id": normalized_id(source, str(real)), "source": source, "name": real.name,
            "path": str(real), "provider_key": None, "size_bytes": size, **info,
        })
    return result


async def scan_all(settings: dict, lm_studio_token: str | None = None) -> list[dict]:
    models: dict[str, dict] = {}
    local_paths: dict[str, str] = {}
    hf = Path(os.environ.get("HF_HUB_CACHE", Path.home() / ".cache" / "huggingface" / "hub"))
    roots: list[tuple[Path, str]] = [(hf, "huggingface_cache")]
    roots += [(Path(p).expanduser(), "custom_folder") for p in settings["models"].get("roots", [])]
    lm_config = settings["models"]["lmStudio"]
    lm_default = Path(lm_config.get("folder") or Path.home() / ".lmstudio" / "models").expanduser()
    roots.append((lm_default, "lm_studio_folder"))
    for root, source in roots:
        scanned = await asyncio.to_thread(scan_root, root, source)
        for item in scanned:
            add_local_model(models, local_paths, item)
    if lm_config.get("enabled", True):
        provider_items = await cli_models()
        provider_items += await api_models(lm_config.get("baseUrl", "http://127.0.0.1:1234"), lm_studio_token)
        merged_provider_keys: set[str] = set()
        for item in provider_items:
            key = item["provider_key"]
            if key in merged_provider_keys:
                continue
            if item.get("path"):
                path_key = canonical_model_path(item["path"])
                local_id = local_paths.get(path_key)
                if local_id:
                    models[local_id]["provider_key"] = key
                    merged_provider_keys.add(key)
                    continue
            provider_name = key.split("/")[-1]
            name_matches = [model for model in models.values()
                            if model.get("path") and normalized_model_name(model["name"]) == normalized_model_name(provider_name)]
            if len(name_matches) == 1:
                name_matches[0]["provider_key"] = key
                merged_provider_keys.add(key)
                continue
            model_id = normalized_id("lm_studio_api", key)
            compatibility = (item.get("compatibility_type") or "").lower()
            fmt = "lmstudio_mlx" if compatibility == "mlx" else "gguf" if "gguf" in compatibility else "provider"
            models[model_id] = {
                "id": model_id, "source": "lm_studio_api", "name": key.split("/")[-1],
                "path": item.get("path"), "provider_key": key, "format": fmt,
                "engine": "lm-studio", "modalities": ["text"], "confidence": 1.0,
                "reason": "LM Studioカタログで検出", "size_bytes": 0,
            }
            merged_provider_keys.add(key)
    return list(models.values())
