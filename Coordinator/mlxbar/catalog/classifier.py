"""Model classification for the catalog and load-time engine re-derivation.

v2.10.0: the previous classifier only routed a model to mlx-vlm when a vision
key AND a processor file existed at the same time. Real vision models in the
wild ship with a vision config but no ``preprocessor_config.json`` /
``processor_config.json`` (or with vision config keys this set did not know),
so they were pinned to ``engine: "mlx-lm"`` in the catalog forever (only a
hard mlx-lm failure ever rescued them, and successful text loads silently
dropped every image). The heuristics are now: architecture family patterns,
processor evidence (files OR ``processor_class``), and vision markers alone;
and :func:`detect_engine` re-derives the engine from a model directory at
load time so stale catalog rows self-heal (``CATALOG_CLASSIFIER_VERSION``
holds the companion bump in ``state.py``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "tokenizer.model")
# Top-level config.json keys that only vision-language models carry. Families
# name these differently (llava: vision_tower / mm_projector_type,
# qwen*_vl: vision_config / image_token_id, gemma3: vision_config,
# internvl/img2seq: image_start_token_id, video families: video_*), so the
# set is deliberately wider than the v2.9.0 five.
VISION_KEYS = {
    "vision_config", "vision_tower", "vision_start_token_id", "visual",
    "vision_feature_select_strategy",
    "image_token_id", "image_token_index", "image_start_token_id",
    "image_seq_length", "image_config", "image_size",
    "mm_projector_type", "mm_vision_tower", "mm_vision_select_layer",
    "video_token_id", "video_config", "video_start_token_id",
}
# Architecture-name markers of families whose serving code lives in mlx-vlm.
# Checked as whole tokens first (split on spaces/underscores/dashes/dots) and
# then as substrings, so "qwen2_vl" matches via the "vl" token while
# "qwen2" / "smollm2" and other text families do not match.
ARCH_VISION_TOKENS = {
    "vl", "vlm", "vision", "visual", "llava", "internvl", "idefics",
    "idefics3", "mllama", "paligemma", "molmo", "pixtral", "minicpmv",
    "smolvlm", "aria", "ovis",
}
ARCH_VISION_MARKERS = (
    "vision", "visual", "vlm", "llava", "internvl", "idefics", "mllama",
    "paligemma", "molmo", "pixtral", "minicpmv", "minicpm-v", "smolvlm",
    "deepseek_vl", "omvlm",
)
# mlx-vlm also ships native implementations for some text-only architectures.
# These models have no vision_config or processor file, so structural VLM
# heuristics alone would incorrectly route them to mlx-lm.
MLX_VLM_TEXT_MODEL_TYPES = {"laguna"}

_TOKEN_SPLIT = re.compile(r"[\s_\-.\+]+")


def _architecture_strings(config: dict) -> list[str]:
    values: list[str] = []
    model_type = config.get("model_type")
    if isinstance(model_type, str) and model_type.strip():
        values.append(model_type)
    architectures = config.get("architectures")
    if isinstance(architectures, list):
        for item in architectures:
            if isinstance(item, str) and item.strip():
                values.append(item)
    return values


def _vlm_architectures(config: dict) -> list[str]:
    """Return the architecture strings that look like a VLM family."""
    matches: list[str] = []
    for raw in _architecture_strings(config):
        lowered = raw.casefold()
        tokens = set(_TOKEN_SPLIT.split(lowered))
        if tokens & ARCH_VISION_TOKENS or any(marker in lowered for marker in ARCH_VISION_MARKERS):
            matches.append(raw)
    return matches


def _processor_class(config: dict, path: Path) -> str | None:
    """Processor evidence from config.json or tokenizer_config.json."""
    value = config.get("processor_class")
    if isinstance(value, str) and value.strip():
        return value
    tokenizer_config_path = path / "tokenizer_config.json"
    if tokenizer_config_path.exists():
        try:
            data = json.loads(tokenizer_config_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        value = data.get("processor_class") if isinstance(data, dict) else None
        if isinstance(value, str) and value.strip():
            return value
    return None


def _vision_evidence(config: dict, path: Path) -> tuple[bool, str | None, bool]:
    has_vision_key = bool(VISION_KEYS.intersection(config))
    processor_class = _processor_class(config, path)
    has_processor = (
        (path / "preprocessor_config.json").exists()
        or (path / "processor_config.json").exists()
        or processor_class is not None
    )
    return has_vision_key, processor_class, has_processor


def _unknown(reason: str, confidence: float = 0.2) -> dict:
    return {"format": "unknown", "engine": None, "modalities": [],
            "confidence": confidence, "reason": reason}


def classify(path: Path) -> dict:
    ggufs = list(path.glob("*.gguf"))
    if ggufs:
        return {"format": "gguf", "engine": "lm-studio", "modalities": ["text"],
                "confidence": 1.0, "reason": "GGUFファイルを検出"}
    config_path = path / "config.json"
    tensors = list(path.glob("*.safetensors"))
    tokenizer = any((path / name).exists() for name in TOKENIZER_FILES)
    if not (config_path.exists() and tensors and tokenizer):
        return _unknown("MLXモデルの必須ファイルが不足")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return _unknown("config.jsonを読み取れない", 0.1)
    has_vision_key, processor_class, has_processor = _vision_evidence(config, path)
    architecture = str(config.get("model_type")
                       or (config.get("architectures") or ["unknown"])[0]).casefold()
    architectures = _architecture_strings(config)
    if any(item.casefold() in MLX_VLM_TEXT_MODEL_TYPES for item in architectures):
        return {"format": "mlx_vlm", "engine": "mlx-vlm", "modalities": ["text"],
                "confidence": 0.95,
                "reason": "mlx-vlm対応テキストモデル: "
                          + (architecture or "architectures")}
    vlm_archs = _vlm_architectures(config)
    if vlm_archs:
        return {"format": "mlx_vlm", "engine": "mlx-vlm",
                "modalities": ["text", "image"], "confidence": 0.95,
                "reason": f"vision系アーキテクチャを検出: {vlm_archs[0]}"}
    if has_vision_key and has_processor:
        return {"format": "mlx_vlm", "engine": "mlx-vlm",
                "modalities": ["text", "image"], "confidence": 0.85,
                "reason": "vision設定とprocessor設定を検出"
                          + (f" ({processor_class})" if processor_class else "")
                          + "（ロード時に再検証）"}
    if has_vision_key:
        # v2.10.0: a vision config without processor files used to be pinned to
        # mlx-lm. Route it to mlx-vlm instead — the mlx-vlm worker also serves
        # the text-only path, and the load-time chain/worker guard recovers
        # if the layout turns out to be unusable there.
        return {"format": "mlx_vlm", "engine": "mlx-vlm",
                "modalities": ["text", "image"], "confidence": 0.6,
                "reason": "vision設定を検出（processor設定ファイルなし・ロード時に再検証）"}
    return {"format": "mlx_lm", "engine": "mlx-lm", "modalities": ["text"],
            "confidence": 0.8, "reason": f"MLXテキストモデル候補: {architecture}"}


def detect_engine(path: Path) -> str | None:
    """Re-derive the native engine (mlx-lm / mlx-vlm) from a model directory.

    Returns None when the directory cannot be judged (unreadable/missing
    config.json); the caller then keeps its existing engine choice. Used at
    load time on the `auto` path so a stale catalog row cannot keep serving a
    vision model through mlx-lm. Mirrors the decision rules of
    :func:`classify` — but unlike :func:`classify` it does not require the
    tokenizer/tensors to already be present, because the caller has a
    specific directory in mind and only needs the vision/text decision.
    """
    config_path = path / "config.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(config, dict):
        return None
    architectures = _architecture_strings(config)
    if any(item.casefold() in MLX_VLM_TEXT_MODEL_TYPES for item in architectures):
        return "mlx-vlm"
    if _vlm_architectures(config):
        return "mlx-vlm"
    has_vision_key, _processor_class, has_processor = _vision_evidence(config, path)
    if has_vision_key and has_processor:
        return "mlx-vlm"
    if has_vision_key:
        return "mlx-vlm"
    return "mlx-lm"
