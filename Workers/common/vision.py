"""Vision-capable model detection for the mlx-lm worker (v2.10.0).

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
"""
from __future__ import annotations

import json
import re
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

_TOKEN_SPLIT = re.compile(r"[\s_\-.\+]+")

GUARD_TAG = "MLXBAR_VLM_GUARD"


def looks_like_vlm(path: str | Path) -> bool:
    """True when the model directory at `path` is a vision-language model."""
    config_path = Path(path) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(config, dict):
        return False
    for value in ([config.get("model_type")] if isinstance(config.get("model_type"), str)
                  else []) + (config.get("architectures") or []):
        if not isinstance(value, str):
            continue
        lowered = value.casefold()
        tokens = set(_TOKEN_SPLIT.split(lowered))
        if tokens & ARCH_VISION_TOKENS or any(marker in lowered for marker in ARCH_VISION_MARKERS):
            return True
    if VISION_KEYS.intersection(config):
        return True
    processor_class = config.get("processor_class")
    if not (isinstance(processor_class, str) and processor_class.strip()):
        tokenizer_config_path = Path(path) / "tokenizer_config.json"
        try:
            data = json.loads(tokenizer_config_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        processor_class = data.get("processor_class") if isinstance(data, dict) else None
    if isinstance(processor_class, str) and processor_class.strip():
        # A processor_class alone usually implies image handling; be
        # conservative and only treat known processor families as vision.
        lowered = processor_class.casefold()
        return any(marker in lowered for marker in ARCH_VISION_MARKERS)
    return False
