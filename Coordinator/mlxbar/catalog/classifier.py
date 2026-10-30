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

v3.2.0 (ISS-2 / WP-210): what a model *can do* and which runtime can *serve*
it are no longer the same decision. ``classify`` additionally returns

* ``capabilities`` -- text/vision/audio/video/tools/reasoning booleans whose
  evidence is the config **and the tensors actually present**. A config that
  declares a vision tower while the checkpoint ships zero ``vision``/``visual``
  tensors is a text model for serving purposes (the real Qwen3.8 Niwaki
  export), and ``language_model_only: true`` is an explicit text-only claim.
* ``runtimeFit`` -- per runtime ``supported`` / ``unsupported`` / ``unknown``.
  This is separate from ``engine``: ``qwen4_exp`` has no mlx-lm
  implementation at all, so its engine is mlx-vlm while its capabilities are
  text-only (vision=false). Deciding the engine from capabilities would send
  it to mlx-lm and fail.
* ``evidence`` -- the facts the two decisions above were made from (tensor
  counts, config keys, processor class, manual overrides), so a wrong badge
  can be explained instead of guessed at.
* ``loaderId`` -- the bundled loader a non-standard artifact needs (ISS-5).
  Detection only; ``mlx_vlm_worker/loaders/`` owns the implementation.

Everything here is additive: the pre-v3.2.0 keys (``format``, ``engine``,
``modalities``, ``confidence``, ``reason``) keep their values and meanings.
"""
from __future__ import annotations

import json
import re
import struct
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
# Architectures that mlx-lm cannot serve at all (independent of vision): the
# family's forward/weight layout only exists in mlx-vlm. `qwen4_exp` is the
# real Qwen3.8-Flash-Next export; mlx-vlm 0.7.10 ships it under
# mlx_vlm/models/qwen4_exp, mlx-lm has no implementation (verified against the
# installed runtimes, 2026-10). Keep this list tiny and evidence-backed --
# every entry adds a "mlx-lm is unsupported" claim to the UI.
MLX_VLM_ONLY_MODEL_TYPES = MLX_VLM_TEXT_MODEL_TYPES | {"qwen4_exp"}
MLX_LM_UNSUPPORTED_MODEL_TYPES = frozenset(MLX_VLM_ONLY_MODEL_TYPES)
# Tensor-name markers for the visual tower. Deliberately the same two
# substrings the checkpoint names actually use (`vision_tower.*`,
# `visual.*`); a bare "image" would also match `image_token_embedding`
# tables that text-only exports keep.
VISION_TENSOR_MARKERS = ("vision", "visual")
# Capability evidence that is not about images. Kept as closed sets so the
# catalog never turns free-form config keys into UI badges.
AUDIO_KEYS = {"audio_config", "audio_tower", "audio_token_id",
              "audio_token_index", "audio_start_token_id", "audio_seq_length"}
VIDEO_KEYS = {"video_config", "video_tower"}
REASONING_KEYS = {"reasoning_config", "reasoning_effort", "enable_thinking"}
TOOL_MARKERS = ("<tool_call", "tool_calls", "tools=", "tool_call_id")
# Config keys that mark a non-standard artifact needing a bundled loader
# (ISS-5). Detection only: this module never imports or executes model code.
BUNDLED_LOADER_KEYS = {"niwaki": "niwaki-qwen4-exp"}
# Cap protected against a corrupt/hostile safetensors header.
MAX_SAFETENSORS_HEADER_BYTES = 64 << 20

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


def _nested_text_config(config: dict) -> dict:
    """The nested `text_config` when it is a mapping, else {}.

    Some exporters move (or duplicate) the language-model section into
    `text_config`; vision/audio markers can appear there instead of at the
    top level. Both levels are inspected.
    """
    value = config.get("text_config")
    return value if isinstance(value, dict) else {}


def _vision_config_keys(config: dict) -> list[str]:
    """Vision markers found at the top level or inside `text_config`."""
    keys = set(config)
    keys |= set(_nested_text_config(config))
    return sorted(VISION_KEYS & keys)


def _audio_config_keys(config: dict) -> list[str]:
    keys = set(config) | set(_nested_text_config(config))
    return sorted(AUDIO_KEYS & keys)


def _video_config_keys(config: dict) -> list[str]:
    keys = set(config) | set(_nested_text_config(config))
    return sorted(VIDEO_KEYS & keys)


def _tokenizer_config(path: Path) -> dict:
    tokenizer_config_path = path / "tokenizer_config.json"
    if not tokenizer_config_path.exists():
        return {}
    try:
        data = json.loads(tokenizer_config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _processor_class(config: dict, path: Path) -> str | None:
    """Processor evidence from config.json or tokenizer_config.json."""
    value = config.get("processor_class")
    if isinstance(value, str) and value.strip():
        return value
    value = _tokenizer_config(path).get("processor_class")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _vision_evidence(config: dict, path: Path) -> tuple[bool, str | None, bool]:
    vision_keys = _vision_config_keys(config)
    processor_class = _processor_class(config, path)
    has_processor = (
        (path / "preprocessor_config.json").exists()
        or (path / "processor_config.json").exists()
        or processor_class is not None
    )
    return bool(vision_keys), processor_class, has_processor


def _safetensors_names(weights: Path) -> list[str]:
    """Tensor names from one .safetensors file, read from its JSON header.

    Reads only the header (length prefix + JSON), never the tensor data, so
    this stays cheap even for a 35 GiB checkpoint. Raises on a malformed file
    so the caller can report "inventory unknown" instead of guessing.
    """
    with weights.open("rb") as handle:
        raw_length = handle.read(8)
        if len(raw_length) != 8:
            raise ValueError("safetensors header truncated")
        length = struct.unpack("<Q", raw_length)[0]
        if not 2 <= length <= MAX_SAFETENSORS_HEADER_BYTES:
            raise ValueError("safetensors header length out of range")
        header = handle.read(length)
    data = json.loads(header)
    if not isinstance(data, dict):
        raise ValueError("safetensors header is not an object")
    return [name for name in data if name != "__metadata__"]


def _tensor_inventory(path: Path) -> dict:
    """Which tensors the checkpoint actually ships, when cheaply knowable.

    Returns ``{"known": bool, "total": int, "vision": int, "source": str}``.
    ``known=False`` means "no index and no readable single-file header" --
    callers must then fall back to config-only evidence rather than treating
    the model as vision-less.
    """
    index_path = path / "model.safetensors.index.json"
    names: list[str] | None = None
    source = ""
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


def _unknown(reason: str, confidence: float = 0.2) -> dict:
    return {"format": "unknown", "engine": None, "modalities": [],
            "confidence": confidence, "reason": reason,
            "capabilities": {"text": False, "vision": False, "audio": False,
                             "video": False, "tools": False, "reasoning": False},
            "runtimeFit": {"mlx-lm": "unknown", "mlx-vlm": "unknown",
                           "omlx": "unknown", "lm-studio": "unknown"},
            "evidence": {"reason": reason}, "loaderId": None}


def _engine_verdict(config: dict, path: Path) -> dict:
    """Decide the serving engine and explain why (shared rule set).

    Kept as one function so :func:`classify` and :func:`detect_engine` can
    never disagree with each other -- the v2.10.0 drift between the catalog
    row and the load-time re-derivation was exactly that bug class.
    """
    architecture = str(config.get("model_type")
                       or (config.get("architectures") or ["unknown"])[0]).casefold()
    architectures = _architecture_strings(config)
    lowered = {item.casefold() for item in architectures}
    if lowered & MLX_VLM_ONLY_MODEL_TYPES:
        return {"engine": "mlx-vlm", "confidence": 0.95,
                "reason": "mlx-vlm専用アーキテクチャを検出: " + (architecture or "architectures"),
                "branch": "vlm_only"}
    if any(item.casefold() in MLX_VLM_TEXT_MODEL_TYPES for item in architectures):
        return {"engine": "mlx-vlm", "confidence": 0.95,
                "reason": "mlx-vlm対応テキストモデル: " + (architecture or "architectures"),
                "branch": "vlm_text"}
    vlm_archs = _vlm_architectures(config)
    if vlm_archs:
        return {"engine": "mlx-vlm", "confidence": 0.95,
                "reason": f"vision系アーキテクチャを検出: {vlm_archs[0]}",
                "branch": "vlm_arch"}
    has_vision_key, processor_class, has_processor = _vision_evidence(config, path)
    language_model_only = bool(config.get("language_model_only"))
    if has_vision_key and has_processor:
        if language_model_only:
            return {"engine": "mlx-lm", "confidence": 0.8,
                    "reason": "language_model_onlyが指定されたテキスト専用モデル"
                              + (f" ({processor_class})" if processor_class else ""),
                    "branch": "language_only"}
        return {"engine": "mlx-vlm", "confidence": 0.85,
                "reason": "vision設定とprocessor設定を検出"
                          + (f" ({processor_class})" if processor_class else "")
                          + "（ロード時に再検証）",
                "branch": "vision_processor"}
    if has_vision_key:
        if language_model_only:
            return {"engine": "mlx-lm", "confidence": 0.8,
                    "reason": "language_model_onlyが指定されたテキスト専用モデル"
                              "（vision設定はあるが画像入力は無効）",
                    "branch": "language_only"}
        # v2.10.0: a vision config without processor files used to be pinned to
        # mlx-lm. Route it to mlx-vlm instead — the mlx-vlm worker also serves
        # the text-only path, and the load-time chain/worker guard recovers
        # if the layout turns out to be unusable there.
        return {"engine": "mlx-vlm", "confidence": 0.6,
                "reason": "vision設定を検出（processor設定ファイルなし・ロード時に再検証）",
                "branch": "vision"}
    return {"engine": "mlx-lm", "confidence": 0.8,
            "reason": f"MLXテキストモデル候補: {architecture}", "branch": "text"}


def _capabilities(config: dict, path: Path, tensors: dict, verdict: dict) -> dict:
    """Feature booleans from config evidence plus the tensors present.

    `vision` requires all three: a vision marker in the config, no
    `language_model_only` claim, and at least one `vision`/`visual` tensor when
    the checkpoint's tensor inventory is readable. That last clause is what
    stops a config that promises a vision tower (Qwen3.8 Niwaki: 0 vision
    tensors out of 3434) from being advertised as image-capable.
    """
    vision_keys = _vision_config_keys(config)
    has_vision_key = bool(vision_keys)
    # NOTE: MLX_VLM_ONLY_MODEL_TYPES is about *serving* (mlx-lm has no
    # implementation), not about vision -- laguna is a text-only family that
    # only mlx-vlm implements. Only real vision markers may claim vision.
    vlm_arch = bool(_vlm_architectures(config))
    language_model_only = bool(config.get("language_model_only"))
    claims_vision = bool(has_vision_key or vlm_arch) and not language_model_only
    if tensors.get("known"):
        vision = bool(claims_vision and int(tensors.get("vision", 0)) > 0)
    else:
        # Inventory unreadable: keep the config-only answer (the historical
        # behaviour) instead of silently claiming a vision-less model.
        vision = claims_vision
    tokenizer_config = _tokenizer_config(path)
    template = tokenizer_config.get("chat_template")
    template_text = template.casefold() if isinstance(template, str) else ""
    tools = any(marker in template_text for marker in TOOL_MARKERS) or bool(
        {"tool_call", "tools"} & (set(config) | set(_nested_text_config(config))))
    reasoning = bool(REASONING_KEYS & (set(config) | set(_nested_text_config(config))))
    capabilities = {
        "text": True,
        "vision": vision,
        "audio": bool(_audio_config_keys(config)) and not language_model_only,
        # A video badge needs a video tower, not just a video token id: the
        # Qwen3-VL family carries video_* tokens in text exports too, and a
        # badge that cannot be honoured is worse than no badge.
        "video": bool(_video_config_keys(config)) and vision and not language_model_only,
        "tools": tools,
        "reasoning": reasoning,
    }
    return capabilities


def _runtime_fit(config: dict, capabilities: dict, fmt: str) -> dict:
    """Per-runtime suitability (v3.2.0 / WP-210).

    ``unsupported`` is a claim this project can back with evidence (a vision
    model in mlx-lm would drop every image; qwen4_exp has no mlx-lm
    implementation). ``unknown`` means "no evidence either way" and must never
    be presented as a yes.
    """
    architectures = {item.casefold() for item in _architecture_strings(config)}
    vlm_only = bool(architectures & MLX_VLM_ONLY_MODEL_TYPES)
    lm_impossible = bool(architectures & MLX_LM_UNSUPPORTED_MODEL_TYPES) or capabilities["vision"]
    fit = {
        "mlx-lm": ("unsupported" if (lm_impossible or fmt == "gguf")
                   else ("supported" if fmt == "mlx_lm" else "unknown")),
        "mlx-vlm": ("supported" if (vlm_only or capabilities["vision"] or fmt == "mlx_vlm")
                    else "unknown"),
        "omlx": "unknown",
        "lm-studio": "supported" if fmt == "gguf" else "unknown",
    }
    return fit


def _loader_id(config: dict) -> str | None:
    """Bundled loader this artifact needs, when it is a known non-standard one.

    Detection is a config-key lookup only: no model-supplied Python is
    imported, scanned or executed here (ISS-5 / AC-5-4).
    """
    for key, loader in BUNDLED_LOADER_KEYS.items():
        if key in config:
            return loader
    return None


def classify(path: Path, *, engine_override: str | None = None,
             capabilities_override: dict | None = None) -> dict:
    """Classify one model directory.

    `engine_override` / `capabilities_override` are the manual escape hatches
    for a wrong automatic verdict. They are additive: the automatic values are
    still computed and recorded in ``evidence`` so an override can be told
    apart from a detection, and no existing key changes shape.
    """
    path = Path(path)
    ggufs = list(path.glob("*.gguf"))
    if ggufs:
        result = {"format": "gguf", "engine": "lm-studio", "modalities": ["text"],
                  "confidence": 1.0, "reason": "GGUFファイルを検出",
                  "capabilities": {"text": True, "vision": False, "audio": False,
                                   "video": False, "tools": False, "reasoning": False},
                  "runtimeFit": {"mlx-lm": "unsupported", "mlx-vlm": "unsupported",
                                 "omlx": "unknown", "lm-studio": "supported"},
                  "evidence": {"format": "gguf", "reason": "GGUFファイルを検出"},
                  "loaderId": None}
        return _apply_overrides(result, engine_override, capabilities_override)
    config_path = path / "config.json"
    tensors_present = list(path.glob("*.safetensors"))
    tokenizer = any((path / name).exists() for name in TOKENIZER_FILES)
    if not (config_path.exists() and tensors_present and tokenizer):
        return _unknown("MLXモデルの必須ファイルが不足")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return _unknown("config.jsonを読み取れない", 0.1)
    if not isinstance(config, dict):
        return _unknown("config.jsonを読み取れない", 0.1)
    verdict = _engine_verdict(config, path)
    tensors = _tensor_inventory(path)
    capabilities = _capabilities(config, path, tensors, verdict)
    fmt = "mlx_vlm" if verdict["engine"] == "mlx-vlm" else "mlx_lm"
    modalities = ["text"] + (["image"] if capabilities["vision"] else [])
    evidence = {
        "architecture": config.get("model_type"),
        "architectures": _architecture_strings(config),
        "visionConfigKeys": _vision_config_keys(config),
        "audioConfigKeys": _audio_config_keys(config),
        "videoConfigKeys": _video_config_keys(config),
        "processorClass": _processor_class(config, path),
        "processorFile": (path / "preprocessor_config.json").exists()
                         or (path / "processor_config.json").exists(),
        "languageModelOnly": bool(config.get("language_model_only")),
        "tensors": tensors,
        "engineBranch": verdict["branch"],
        "engineReason": verdict["reason"],
        "loaderHint": _loader_id(config),
    }
    result = {
        "format": fmt,
        "engine": verdict["engine"],
        # A vision-less mlx-vlm export (config promises a tower, tensors say
        # otherwise) is served through mlx-vlm's text path: the engine stays
        # mlx-vlm because the architecture requires it, while modalities and
        # capabilities say "text only".
        "modalities": modalities,
        "confidence": verdict["confidence"],
        "reason": verdict["reason"],
        "capabilities": capabilities,
        "runtimeFit": _runtime_fit(config, capabilities, fmt),
        "evidence": evidence,
        "loaderId": _loader_id(config),
    }
    return _apply_overrides(result, engine_override, capabilities_override)


def _apply_overrides(result: dict, engine_override: str | None,
                     capabilities_override: dict | None) -> dict:
    """Apply manual overrides and record them in ``evidence`` (additive)."""
    if not engine_override and not capabilities_override:
        return result
    evidence = dict(result.get("evidence") or {})
    overrides: dict = {}
    if engine_override:
        overrides["engine"] = {"from": result.get("engine"), "to": engine_override}
        result["engine"] = engine_override
        if result.get("format") in {"mlx_lm", "mlx_vlm"}:
            result["format"] = "mlx_vlm" if engine_override == "mlx-vlm" else "mlx_lm"
    if isinstance(capabilities_override, dict):
        merged = dict(result.get("capabilities") or {})
        applied = {key: bool(value) for key, value in capabilities_override.items()
                   if key in merged}
        merged.update(applied)
        overrides["capabilities"] = applied
        result["capabilities"] = merged
        if "vision" in applied:
            result["modalities"] = ["text"] + (["image"] if merged["vision"] else [])
    evidence["overrides"] = overrides
    result["evidence"] = evidence
    if "capabilities" in overrides:
        result["runtimeFit"] = _runtime_fit(
            {"model_type": evidence.get("architecture"),
             "architectures": evidence.get("architectures") or []},
            result["capabilities"], str(result.get("format") or ""))
    return result


def detect_engine(path: Path, *, engine_override: str | None = None) -> str | None:
    """Re-derive the native engine (mlx-lm / mlx-vlm) from a model directory.

    Returns None when the directory cannot be judged (unreadable/missing
    config.json); the caller then keeps its existing engine choice. Used at
    load time on the `auto` path so a stale catalog row cannot keep serving a
    vision model through mlx-lm. Shares :func:`_engine_verdict` with
    :func:`classify` — but unlike `classify` it does not require the
    tokenizer/tensors to already be present, because the caller has a
    specific directory in mind and only needs the engine decision.
    """
    if engine_override:
        return engine_override
    config_path = Path(path) / "config.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(config, dict):
        return None
    return _engine_verdict(config, Path(path))["engine"]
