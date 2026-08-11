from __future__ import annotations

import json
from pathlib import Path


TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "tokenizer.model")
VISION_KEYS = {"vision_config", "vision_tower", "image_token_id", "mm_projector_type", "visual"}
# mlx-vlm also ships native implementations for some text-only architectures.
# These models have no vision_config or processor file, so structural VLM
# heuristics alone would incorrectly route them to mlx-lm.
MLX_VLM_TEXT_MODEL_TYPES = {"laguna"}


def classify(path: Path) -> dict:
    ggufs = list(path.glob("*.gguf"))
    if ggufs:
        return {"format": "gguf", "engine": "lm-studio", "modalities": ["text"],
                "confidence": 1.0, "reason": "GGUFファイルを検出"}
    config_path = path / "config.json"
    tensors = list(path.glob("*.safetensors"))
    tokenizer = any((path / name).exists() for name in TOKENIZER_FILES)
    if not (config_path.exists() and tensors and tokenizer):
        return {"format": "unknown", "engine": None, "modalities": [], "confidence": 0.2,
                "reason": "MLXモデルの必須ファイルが不足"}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {"format": "unknown", "engine": None, "modalities": [], "confidence": 0.1,
                "reason": "config.jsonを読み取れない"}
    has_vision_key = bool(VISION_KEYS.intersection(config))
    has_processor = (path / "preprocessor_config.json").exists() or (path / "processor_config.json").exists()
    architecture = str(config.get("model_type") or (config.get("architectures") or ["unknown"])[0]).casefold()
    if architecture in MLX_VLM_TEXT_MODEL_TYPES:
        return {"format": "mlx_vlm", "engine": "mlx-vlm", "modalities": ["text"],
                "confidence": 0.95, "reason": f"mlx-vlm対応テキストモデル: {architecture}"}
    if has_vision_key and has_processor:
        return {"format": "mlx_vlm", "engine": "mlx-vlm", "modalities": ["text", "image"],
                "confidence": 0.85, "reason": "vision設定とprocessor設定を検出（ロード時に再検証）"}
    return {"format": "mlx_lm", "engine": "mlx-lm", "modalities": ["text"],
            "confidence": 0.8, "reason": f"MLXテキストモデル候補: {architecture}"}
