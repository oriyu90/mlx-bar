"""JANG (jangtq2 / JANGH2) artifact detection shared by both workers.

v5.0.1: Ornith-1.5-122B-JANGH2 and similar JANG exports ship a
``jang_config.json`` (``format == "jangtq2"`` or ``weight_format`` containing
``jangtq``), a ``config.json`` whose (possibly nested) ``quantization`` carries
``mode == "jangtq2"``, or an ``osaurus.json`` marker. None of the runtimes
MLXBar can start (mlx-lm / mlx-vlm stock) understands that layout, so the
models must fail *immediately* with ``MODEL_JANG_OSAURUS_REQUIRED`` instead of
dying later inside stock weight loading (``SwitchLinear.to_quantized``
rotation errors are the observed symptom).

This module is deliberately small and self-contained: it reads only
``config.json`` / ``jang_config.json`` and the *existence* of
``osaurus.json``. It never reads safetensors weights, never imports or runs
model-supplied ``*.py``, and never touches the coordinator package -- the
workers run in their own interpreter/venv. The coordinator's
``catalog/classifier.py`` mirrors the same three rules (same duplication
pattern as ``common/vision.py``) so catalog classification and the worker
preflight can never disagree about whether a model is JANG.

Matching is narrow on purpose: ``jangtq2`` exact (case-insensitive) for
format/mode, ``jangtq`` substring for ``weight_format`` only, and file
existence for ``osaurus.json``. A normal affine-quantized model (e.g.
``qwen3_5_moe``) can never match.
"""
from __future__ import annotations

import json
from pathlib import Path

from .model_errors import ModelLoadError

JANG_MODE = "jangtq2"
JANG_WEIGHT_MARKER = "jangtq"
JANG_UNSUPPORTED_MESSAGE = (
    "このモデルの量子化（JANG jangtq2）は MLXBar では実行できません。"
    "Osaurus 0.25.24+（または対応する MLX Studio / jang-tools）で開いてください。"
    "LM Studio と oMLX は JANG に対応していないため使えません"
)


def _contains_jang_quant_mode(obj: object, _depth: int = 0) -> bool:
    """True when a (possibly nested) ``quantization`` dict has mode jangtq2.

    Walks the whole config so ``text_config.quantization.mode`` and friends
    are found at any depth. Only a dict under a ``quantization`` key counts,
    and its ``mode`` must match ``jangtq2`` exactly (case-insensitive).
    Depth is capped so a hostile deeply-nested config cannot overflow the
    stack (MEDIUM-3); beyond the cap the answer is simply "not found".
    """
    if _depth > 20:
        return False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "quantization" and isinstance(value, dict):
                mode = value.get("mode")
                if isinstance(mode, str) and mode.strip().casefold() == JANG_MODE:
                    return True
            if _contains_jang_quant_mode(value, _depth + 1):
                return True
    elif isinstance(obj, list):
        return any(_contains_jang_quant_mode(item, _depth + 1) for item in obj)
    return False


def jang_evidence(config: dict | None, path: str | Path) -> str | None:
    """Short evidence string when ``path`` is a JANG artifact, else None.

    The string names only the *relative* file and the matched field --
    never an absolute path -- so it is safe to put in an error ``detail``.
    """
    path = Path(path)
    evidences: list[str] = []
    # (a) jang_config.json: format == "jangtq2" or weight_format contains "jangtq"
    jang_path = path / "jang_config.json"
    try:
        if jang_path.exists():
            try:
                data = json.loads(jang_path.read_text(encoding="utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict):
                fmt = data.get("format")
                if isinstance(fmt, str) and fmt.strip().casefold() == JANG_MODE:
                    evidences.append(f"jang_config.json format={fmt.strip()}")
                weight_format = data.get("weight_format")
                if isinstance(weight_format, str) and JANG_WEIGHT_MARKER in weight_format.casefold():
                    evidences.append("jang_config.json weight_format contains jangtq")
    except OSError:
        pass
    # (b) config.json quantization mode == "jangtq2" (recursive)
    if isinstance(config, dict) and _contains_jang_quant_mode(config):
        evidences.append("config.json quantization mode=jangtq2")
    # (c) osaurus.json exists
    try:
        if (path / "osaurus.json").exists():
            evidences.append("osaurus.json 存在")
    except OSError:
        pass
    if not evidences:
        return None
    return "; ".join(evidences)


def is_jang_artifact(config: dict | None, path: str | Path) -> bool:
    """True when the directory is a JANG artifact (see :func:`jang_evidence`)."""
    return jang_evidence(config, path) is not None


def raise_if_jang(config: dict | None, path: str | Path) -> None:
    """Raise ``MODEL_JANG_OSAURUS_REQUIRED`` when ``path`` is a JANG artifact.

    Raises nothing for normal artifacts. The message never carries an
    absolute path (TASK-003 REVIEW F-04); the short evidence rides in
    ``detail`` and as a trailing parenthetical in the message so the
    coordinator's ``detail=text[-1000:]`` fallback still shows it.
    """
    evidence = jang_evidence(config, path)
    if evidence is None:
        return
    raise ModelLoadError(
        "MODEL_JANG_OSAURUS_REQUIRED",
        f"{JANG_UNSUPPORTED_MESSAGE}（証拠: {evidence}）",
        detail=evidence)
