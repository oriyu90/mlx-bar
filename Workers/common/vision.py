"""Vision-capable model detection for the mlx-lm worker (v2.10.0 / v3.2.0).

Self-contained mirror of ``Coordinator/mlxbar/catalog/classifier.py`` --
the workers run in their own interpreter/venv and cannot import the
coordinator package, so the marker sets are duplicated here on purpose.
``Tests/test_v2_10_engine_detection.py`` checks that the two marker sets stay
in sync so the coordinator re-derivation and this guard never disagree.

The guard exists so a model that can be *text-loaded* by mlx-lm (gemma3,
qwen3, and other families that may text-load a vision checkpoint) is never
silently served by the mlx-lm worker: an image request would just be dropped
end-to-end. The coordinator auto path never reaches this worker for such a
model (re-derivation + candidate chain), and an explicit ``allow_vlm`` request
(user picked "mlx-lm" on purpose) skips the guard.

v3.2.0 (ISS-2 / WP-210): "does the config mention a vision tower" is not the
same question as "can this checkpoint accept images". The decision now also
requires that the checkpoint actually ships ``vision``/``visual`` tensors (the
real Qwen3.8 Niwaki export declares a vision_config and ships zero vision
tensors out of 3434), and honours an explicit ``language_model_only: true``.
The same rules as the coordinator's classifier, so
:func:`verify` can assert that both sides agree for one directory.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

VISION_KEYS = frozenset({
    "vision_config", "vision_tower", "vision_start_token_id", "visual",
    "vision_feature_select_strategy",
    "image_token_id", "image_token_index", "image_start_token_id",
    "image_seq_length", "image_config", "image_size",
    "mm_projector_type", "mm_vision_tower", "mm_vision_select_layer",
    "video_token_id", "video_config", "video_start_token_id",
})
ARCH_VISION_TOKENS = frozenset({
    "vl", "vlm", "vision", "visual", "llava", "internvl", "idefics",
    "idefics3", "mllama", "paligemma", "molmo", "pixtral", "minicpmv",
    "smolvlm", "aria", "ovis",
})
ARCH_VISION_MARKERS = (
    "vision", "visual", "vlm", "llava", "internvl", "idefics", "mllama",
    "paligemma", "molmo", "pixtral", "minicpmv", "minicpm-v", "smolvlm",
    "deepseek_vl", "omvlm",
)
# Kept in step with the coordinator: architectures mlx-vlm implements and
# mlx-lm does not. Only used for messaging/verification here (the guard is
# about images), but a drift would make `verify()` disagree.
MLX_VLM_TEXT_MODEL_TYPES = frozenset({"laguna"})
MLX_VLM_ONLY_MODEL_TYPES = frozenset(MLX_VLM_TEXT_MODEL_TYPES | {"qwen4_exp"})
VISION_TENSOR_MARKERS = ("vision", "visual")

_MAX_SAFETENSORS_HEADER_BYTES = 64 << 20

_TOKEN_SPLIT = re.compile(r"[\s_\-.\+]+")

GUARD_TAG = "MLXBAR_VLM_GUARD"


def _nested_text_config(config: dict) -> dict:
    value = config.get("text_config")
    return value if isinstance(value, dict) else {}


def _vision_config_keys(config: dict) -> list[str]:
    keys = set(config) | set(_nested_text_config(config))
    return sorted(VISION_KEYS & keys)


def _architecture_strings(config: dict) -> list[str]:
    values: list[str] = []
    if isinstance(config.get("model_type"), str):
        values.append(config["model_type"])
    for item in config.get("architectures") or []:
        if isinstance(item, str):
            values.append(item)
    return values


def _vlm_architecture(config: dict) -> bool:
    for value in _architecture_strings(config):
        lowered = value.casefold()
        tokens = set(_TOKEN_SPLIT.split(lowered))
        if tokens & ARCH_VISION_TOKENS or any(marker in lowered for marker in ARCH_VISION_MARKERS):
            return True
    return False


def _safetensors_names(weights: Path) -> list[str]:
    with weights.open("rb") as handle:
        raw_length = handle.read(8)
        if len(raw_length) != 8:
            raise ValueError("safetensors header truncated")
        length = struct.unpack("<Q", raw_length)[0]
        if not 2 <= length <= _MAX_SAFETENSORS_HEADER_BYTES:
            raise ValueError("safetensors header length out of range")
        header = handle.read(length)
    data = json.loads(header)
    if not isinstance(data, dict):
        raise ValueError("safetensors header is not an object")
    return [name for name in data if name != "__metadata__"]


def tensor_inventory(path: str | Path) -> dict:
    """``{"known", "total", "vision", "source"}`` for one model directory.

    Reads only an index JSON or a safetensors *header*, never tensor data, so
    it costs no meaningful memory even for a 35 GiB checkpoint.
    """
    path = Path(path)
    names: list[str] | None = None
    source = ""
    index_path = path / "model.safetensors.index.json"
    if index_path.exists():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            weight_map = data.get("weight_map") if isinstance(data, dict) else None
            if isinstance(weight_map, dict) and weight_map:
                names = [str(key) for key in weight_map]
                source = "model.safetensors.index.json"
        except Exception:
            names = None
    if names is None:
        for weights in sorted(path.glob("*.safetensors")):
            try:
                names = _safetensors_names(weights)
                source = weights.name
                break
            except Exception:
                continue
    if names is None:
        return {"known": False, "total": 0, "vision": 0, "source": ""}
    vision = sum(1 for name in names
                 if any(marker in name.casefold() for marker in VISION_TENSOR_MARKERS))
    return {"known": True, "total": len(names), "vision": vision, "source": source}


def inspect(path: str | Path) -> dict:
    """Worker-side capability reading, mirroring the coordinator's rules."""
    path = Path(path)
    try:
        config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {"readable": False, "vision": False, "text": False,
                "languageModelOnly": None, "tensors": {"known": False},
                "visionConfigKeys": [], "architectures": [],
                "reason": "config.jsonを読み取れない"}
    if not isinstance(config, dict):
        return {"readable": False, "vision": False, "text": False,
                "languageModelOnly": None, "tensors": {"known": False},
                "visionConfigKeys": [], "architectures": [],
                "reason": "config.jsonがオブジェクトではない"}
    vision_keys = _vision_config_keys(config)
    language_model_only = bool(config.get("language_model_only"))
    architectures = _architecture_strings(config)
    vlm_arch = _vlm_architecture(config)
    vlm_only = bool({item.casefold() for item in architectures} & MLX_VLM_ONLY_MODEL_TYPES)
    # vlm_only is about serving, not images: laguna is text-only and only
    # mlx-vlm implements it, so it must not claim vision.
    claims_vision = bool(vision_keys or vlm_arch) and not language_model_only
    tensors = tensor_inventory(path)
    if tensors["known"]:
        vision = bool(claims_vision and tensors["vision"] > 0)
    else:
        vision = claims_vision
    reason = "vision_config等を検出"
    if language_model_only:
        reason = "language_model_onlyが指定されている"
    elif tensors["known"] and claims_vision and tensors["vision"] == 0:
        reason = "vision_configはあるがvisionテンソルが0件"
    elif not claims_vision:
        reason = "vision設定を検出しない"
    return {
        "readable": True,
        "vision": vision,
        "text": True,
        "languageModelOnly": language_model_only,
        "tensors": tensors,
        "visionConfigKeys": vision_keys,
        "architectures": architectures,
        "vlmOnlyArchitecture": vlm_only,
        "reason": reason,
    }


def looks_like_vlm(path: str | Path) -> bool:
    """True when the model directory at `path` is a vision-language model."""
    return bool(inspect(path).get("vision"))


def verify(coordinator_capabilities: dict | None, path: str | Path) -> dict:
    """Check the coordinator's `capabilities` against this worker's reading.

    Returns ``{"agree": bool, "worker": {...}, "coordinator": {...},
    "differences": [...]}``. Never raises and never changes state: the
    coordinator remains the single source of truth (its row decides routing);
    this is the check that the two implementations have not drifted, which
    AC-2-4 pins for one directory.
    """
    worker = inspect(path)
    coordinator = coordinator_capabilities if isinstance(coordinator_capabilities, dict) else {}
    differences: list[str] = []
    if coordinator:
        for key in ("text", "vision"):
            if key not in coordinator:
                continue
            if bool(coordinator.get(key)) != bool(worker.get(key)):
                differences.append(
                    f"{key}: coordinator={bool(coordinator.get(key))} worker={bool(worker.get(key))}")
    return {
        "agree": not differences,
        "worker": worker,
        "coordinator": {"text": coordinator.get("text"), "vision": coordinator.get("vision")},
        "differences": differences,
    }
