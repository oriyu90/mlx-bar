"""Bundled loader for the Qwen3.8-Flash-Next "niwaki" MLX export (ISS-5).

The real artifact (``Qwen3.8-Flash-Next-102B-A5B-Niwaki-v2.4-3bit-mlx``) is a
``qwen4_exp`` model whose config carries a ``niwaki`` block (``kept_experts``
/ ``shared_only_layers`` / ``kept_routing``) and whose weights are compressed
expert banks plus PLE 2-bit pair rows. mlx-vlm 0.7.10 implements
``qwen4_exp``, but its ``load_weights`` does not understand that layout, so
the weights silently do not land. The artifact also ships a
``niwaki_flash_load.py`` that hooks ``Model.load_weights`` -- **that file is
never executed here** (a downloaded model is not a trusted code source).

Status: **detection + preflight + weight reconstruction are implemented**
(``LoaderSpec.implemented = True``). The reconstruction re-implements the
three hooks from the artifact's declared ``config.json`` evidence only
(AC-5-4): compact ``kept_experts`` banks (masked routing over the full
router), ``shared_only_layers`` (routed bank deleted), and PLE 2-bit
pair-row n-gram tables.
"""
from __future__ import annotations

import json
from pathlib import Path

from common.model_errors import ModelLoadError
from common.vision import tensor_inventory

from .base import BundledLoader, LoaderSpec

REQUIRED_MODEL_TYPE = "qwen4_exp"
REQUIRED_ARCHITECTURE = "Qwen4ExpForConditionalGeneration"
MODEL_SUPPLIED_LOADER = "niwaki_flash_load.py"


class NiwakiQwen4ExpLoader(BundledLoader):
    spec = LoaderSpec(
        loader_id="niwaki-qwen4-exp",
        display_name="Niwaki qwen4_exp",
        description=(
            "Qwen3.8-Flash-Next(102B-A5B) の Niwaki 3bit 版。kept_experts による"
            "専門家バンク圧縮と PLE 2bit ペア行を再構成する同梱ローダ。"
            "モデル同梱の .py は実行しない"),
        model_types=(REQUIRED_MODEL_TYPE,),
        config_keys=("niwaki",),
        implemented=True,
    )

    def preflight(self, path: str | Path) -> dict:
        path = Path(path)
        checks: list[dict] = []

        def record(name: str, ok: bool, detail: str = "", **extra) -> None:
            entry = {"name": name, "ok": ok, "detail": detail}
            entry.update(extra)
            checks.append(entry)

        config = self._config(path)
        record("config", True, "config.json を読み取り")
        model_type = str(config.get("model_type") or "").casefold()
        if model_type != REQUIRED_MODEL_TYPE:
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                f"{self.spec.display_name} は model_type='{REQUIRED_MODEL_TYPE}' 専用です"
                f"（検出: {model_type or '未設定'}）",
                detail=str(path))
        record("modelType", True, model_type)

        architectures = [str(item) for item in (config.get("architectures") or [])]
        if architectures and REQUIRED_ARCHITECTURE not in architectures:
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                f"想定アーキテクチャ {REQUIRED_ARCHITECTURE} と一致しません"
                f"（検出: {', '.join(architectures)}）",
                detail=str(path))
        record("architecture", True, ", ".join(architectures) or REQUIRED_ARCHITECTURE)

        niwaki = config.get("niwaki")
        if not isinstance(niwaki, dict):
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                "config.json に niwaki ブロックがありません（このローダの対象外です）",
                detail=str(path))
        kept_experts = niwaki.get("kept_experts")
        if not isinstance(kept_experts, dict) or not kept_experts:
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                "niwaki.kept_experts が無い/空です。圧縮された専門家バンクを再構成できません",
                detail=str(path))
        text_config = config.get("text_config")
        layers = text_config.get("num_hidden_layers") if isinstance(text_config, dict) else None
        invalid_layers = []
        if isinstance(layers, int) and layers > 0:
            for key in kept_experts:
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    invalid_layers.append(str(key))
                    continue
                if not 0 <= index < layers:
                    invalid_layers.append(str(key))
        if invalid_layers:
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                f"niwaki.kept_experts の層番号が範囲外です（{', '.join(invalid_layers[:5])}）",
                detail=f"num_hidden_layers={layers}")
        record("niwaki.kept_experts", True,
               f"{len(kept_experts)}層 / shared_only_layers="
               f"{len(niwaki.get('shared_only_layers') or [])}",
               keptExpertLayers=len(kept_experts))

        quantization = config.get("quantization")
        bits = quantization.get("bits") if isinstance(quantization, dict) else None
        if isinstance(bits, int) and not isinstance(bits, bool) and bits not in {2, 3, 4, 8}:
            raise ModelLoadError(
                "MODEL_QUANT_UNSUPPORTED",
                f"量子化ビット数 {bits} はこのローダの対象外です（2/3/4/8 bit のみ）",
                detail=json.dumps(quantization)[:500])
        record("quantization", True, f"{bits} bit" if bits is not None else "未指定")

        tensors = tensor_inventory(path)
        if tensors.get("known") and int(tensors.get("total", 0)) <= 0:
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                "重みテンソルが見つかりません（ダウンロードが不完全です）",
                detail=str(path))
        record("tensors", bool(tensors.get("known")), str(tensors.get("source") or ""),
               total=tensors.get("total"), vision=tensors.get("vision"))
        if tensors.get("known") and int(tensors.get("vision", 0)) == 0:
            # Expected for this export: the config declares a vision tower that
            # the checkpoint does not ship. Text-only serving is the documented
            # outcome (capabilities.vision=false), not an error.
            record("visionTower", True,
                   "config は vision を宣言するが vision テンソル 0 件（テキスト経路で serving）")

        tokenizer_present = any((path / name).exists() for name in
                                ("tokenizer.json", "tokenizer_config.json", "tokenizer.model"))
        if not tokenizer_present:
            raise ModelLoadError(
                "MODEL_TOKENIZER_MISSING",
                "tokenizer ファイルが見つかりません",
                detail=str(path))
        record("tokenizer", True)

        # Record (never run) the model-supplied script so a support report shows
        # we saw it and deliberately ignored it.
        supplied = (path / MODEL_SUPPLIED_LOADER).exists()
        record("modelSuppliedLoader", True,
               f"{MODEL_SUPPLIED_LOADER} は同梱物のため実行しない" if supplied
               else "モデル同梱ローダなし")
        return {
            "loaderId": self.spec.loader_id,
            "implemented": self.spec.implemented,
            "checks": checks,
            "modelSuppliedLoader": MODEL_SUPPLIED_LOADER if supplied else None,
        }

    @staticmethod
    def _config(path: Path) -> dict:
        path = Path(path)
        try:
            config = json.loads((path / "config.json").read_text(encoding="utf-8"))
        except Exception as exc:
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                "config.json を読み取れません",
                detail=f"{path / 'config.json'}: {exc}") from exc
        if not isinstance(config, dict):
            raise ModelLoadError("MODEL_FORMAT_UNSUPPORTED",
                                 "config.json がオブジェクトではありません", detail=str(path))
        return config

    def load_impl(self, path: Path, *, trust_remote_code: bool = False):
        path = Path(path)
        from mlx_vlm.models import switch_layers as sw
        from mlx_vlm.utils import load_model, load_processor

        config = self._config(path)
        tc = config.get("text_config") if isinstance(
            config.get("text_config"), dict) else config
        hidden_size = int(tc.get("hidden_size", 0))
        if hidden_size <= 0:
            raise ModelLoadError(
                "MODEL_FORMAT_UNSUPPORTED",
                "hidden_size を config から読めません",
                detail=str(path))
        niwaki = config.get("niwaki") if isinstance(
            config.get("niwaki"), dict) else {}
        skip_layers = {int(i) for i in niwaki.get("shared_only_layers", []) or []}
        kept = {int(i): [int(e) for e in v]
                for i, v in (niwaki.get("kept_experts", {}) or {}).items()}
        full_experts = int(tc.get("num_experts", 0) or 0)
        pair = int(tc.get("niwaki_ple_pair", 1) or 1)
        ple_quant = tc.get("niwaki_ple_quant") if isinstance(
            tc.get("niwaki_ple_quant"), dict) else {}

        from mlx_vlm.models.qwen4_exp import qwen4_exp as Q

        if getattr(Q.Model, "_mlxbar_niwaki_hooked", False):
            raise ModelLoadError(
                "MODEL_LOAD_FAILED",
                "同梱ローダのフックが既に適用されています",
                detail=self.spec.loader_id)
        orig_load_weights = Q.Model.load_weights

        def _load_weights_hooked(model, weights, strict=True):
            layers = model.language_model.model.layers
            # (1) Routed-bank deletion.
            for index in sorted(skip_layers):
                if 0 <= index < len(layers):
                    layers[index].mlp.switch_mlp = _ZeroSwitch(hidden_size)
            drop_markers = tuple(
                f".layers.{i}.mlp.switch_mlp." for i in skip_layers)
            if drop_markers:
                weights = [(k, v) for k, v in weights
                           if not any(m in k for m in drop_markers)]
            # (2) Compact banks + masked routing.
            if kept:
                import numpy as _np  # noqa: F401 (used in helper)
                del _np
                weight_dict = dict(weights)
                for index, expert_ids in kept.items():
                    if not 0 <= index < len(layers):
                        continue
                    block = layers[index].mlp
                    old = block.switch_mlp.gate_proj
                    gate_key = next(
                        (k for k in weight_dict
                         if f".layers.{index}.mlp.switch_mlp.gate_proj.weight" in k),
                        None)
                    if gate_key is None:
                        continue
                    scales_key = gate_key.replace(".weight", ".scales")
                    bank_hidden = int(weight_dict[scales_key].shape[1]) \
                        if scales_key in weight_dict else int(
                            old.gate_proj.weight.shape[1])
                    block.switch_mlp = sw.SwitchGLU(
                        hidden_size, bank_hidden, len(expert_ids))
                    if hasattr(old, "bits"):
                        import mlx.nn as nn
                        nn.quantize(
                            block.switch_mlp,
                            group_size=old.group_size, bits=old.bits)
                    _apply_compact_mask(
                        block, expert_ids,
                        full_experts if full_experts > 0 else None)
            # (3) PLE pair rows.
            if pair > 1:
                group = int(ple_quant.get("group_size", 128))
                bits = int(ple_quant.get("bits", 2))
                weight_dict = dict(weights)
                for layer in layers:
                    ple = getattr(layer, "ple", None)
                    emb = getattr(getattr(ple, "ple_embedding", None),
                                  "ngram_embedding", None) if ple else None
                    if emb is None or not hasattr(emb, "shards"):
                        continue
                    dims = getattr(emb, "dims", 0) or 0
                    if dims <= 0:
                        continue
                    new_shards = []
                    for n, _ in enumerate(emb.shards):
                        key = next(
                            (k for k in weight_dict
                             if k.endswith(f".ngram_embedding.shards.{n}.weight")
                             and ".ple." in k),
                            None)
                        if key is None:
                            break
                        packed_rows = int(weight_dict[key].shape[0])
                        new_shards.append(_PairedQuantizedEmbedding(
                            packed_rows, dims * pair, group, bits, pair, dims))
                    else:
                        emb.shards = new_shards
                        continue
                    break
            return orig_load_weights(model, weights, strict=strict)

        Q.Model.load_weights = _load_weights_hooked
        Q.Model._mlxbar_niwaki_hooked = True
        try:
            model = load_model(path, lazy=True, strict=True)
            import mlx.core as mx

            mx.eval(model.parameters())
            model.eval()
            processor = load_processor(path)
            _extend_detokenizer_tokenmap(model, processor)
            return model, processor
        finally:
            Q.Model.load_weights = orig_load_weights
            Q.Model._mlxbar_niwaki_hooked = False

# Module-level helpers for load_impl (repository code, never model-supplied).
# ---------------------------------------------------------------------------

def _extend_detokenizer_tokenmap(model, processor) -> None:
    """Extend the streaming detokenizer's token map to the lm_head width.

    The niwaki export's ``tokenizer.json`` stops at id 248043, but its
    ``lm_head`` (and ``embed_tokens``) span 248320 rows: four chat/thinking
    special tokens (``<|im_start|>`` 248045, ``<|im_end|>`` 248046,
    ``<think>`` 248068, ``</think>`` 248069) live above the tokenizer
    vocabulary. mlx-vlm builds the detokenizer map from the tokenizer, so
    generating any of those ids raises ``IndexError``. Extending the map
    with the decoded surface form (falling back to the replacement
    character) keeps generation alive; it changes no weights.
    """
    try:
        detok = getattr(processor, "detokenizer", None)
        tokenmap = getattr(detok, "tokenmap", None)
        if not isinstance(tokenmap, list):
            return
        head = getattr(getattr(model, "language_model", None), "lm_head", None)
        weight = getattr(head, "weight", None)
        width = int(weight.shape[0]) if weight is not None else 0
        if width <= len(tokenmap):
            return
        tok = getattr(processor, "tokenizer", processor)
        # NOTE: the filler must exist in mlx-vlm's BPE byte decoder
        # (``_byte_decoder`` only covers byte-level characters). "?" (0x3F)
        # is always present; the U+FFFD replacement character is not.
        fallback = "?"
        try:
            unk = tok.decode([tok.unk_token_id]) \
                if getattr(tok, "unk_token_id", None) is not None else ""
            if unk and len(unk) == 1:
                fallback = unk
        except Exception:
            pass
        extra = []
        for token_id in range(len(tokenmap), width):
            try:
                surface = tok.decode([token_id])
            except Exception:
                surface = ""
            extra.append(surface or fallback)
        tokenmap.extend(extra)
    except Exception:
        # A best-effort robustness shim: never break a successful load.
        return

def _apply_compact_mask(block, expert_ids: list[int], full_experts: int | None) -> None:
    """Masked routing over the full router for a compact expert bank.

    ``block.switch_mlp`` holds only ``len(expert_ids)`` experts (in config
    order). The router still scores every expert; only stored experts keep
    their renormalised top-k weight, the rest contribute 0. ``_remap`` maps
    full-router indices to compact-bank rows (unstored experts point at row
    0, whose contribution is multiplied by 0).
    """
    import mlx.core as mx
    import numpy as np

    n_full = int(full_experts) if full_experts else int(max(expert_ids) + 1)
    cls = type(block)

    class CompactMasked(cls):
        def __call__(self, x):
            gates = mx.softmax(self.gate(x), axis=-1, precise=True)
            k = self.top_k
            inds = mx.argpartition(gates, kth=-k, axis=-1)[..., -k:]
            scores = mx.take_along_axis(gates, inds, axis=-1)
            scores = scores / scores.sum(axis=-1, keepdims=True)
            scores = scores * mx.take(
                self._kept_mask, inds).astype(scores.dtype)
            y = (self.switch_mlp(
                x, mx.take(self._remap, inds)) * scores[..., None]).sum(axis=-2)
            return (y + mx.sigmoid(self.shared_expert_gate(x))
                    * self.shared_expert(x))

    CompactMasked.__name__ = cls.__name__ + "CompactMasked"
    block.__class__ = CompactMasked
    mask = np.zeros(n_full, bool)
    mask[np.array(expert_ids)] = True
    remap = np.zeros(n_full, np.int32)
    remap[np.array(expert_ids)] = np.arange(len(expert_ids))
    block._kept_mask, block._remap = mx.array(mask), mx.array(remap)
    return None


class _ZeroSwitch:
    """Routed path of a deleted bank: owns nothing, contributes nothing."""

    def __init__(self, hidden: int):
        self._hidden = int(hidden)

    def __call__(self, x, indices, *args, **kwargs):
        import mlx.core as mx
        return mx.zeros((*indices.shape, self._hidden), dtype=x.dtype)


try:
    import mlx.nn as _nn
except Exception:  # noqa: BLE001 -- test env without the mlx runtime
    _nn = None


class _PairedQuantizedEmbedding(_nn.Module if _nn is not None else object):
    """PLE shard stored as ``[rows/p, dims*p]`` packed rows.

    Lookup fetches the packed row for ``idx // pair`` (dequantized from the
    2-bit packed weight with its scales/biases, which ``load_weights`` fills
    in by parameter name), then slices the ``idx % pair``-th piece of width
    ``dims``. Parameter names match the stock ``ShardedEmbedding`` shards
    (``weight``/``scales``/``biases``) so the checkpoint keys land here.

    ``nn.Module`` subclass: replacing ``emb.shards[n]`` with this module
    gives the stock ``load_weights`` a destination for the three
    per-shard keys (the stock ``nn.Embedding`` shard only owns
    ``weight``). The module is installed on the already-built ``emb.shards``
    list before the stock ``load_weights`` runs.
    """

    def __init__(self, packed_rows: int, packed_dim: int, group_size: int,
                 bits: int, pair: int, dims: int):
        import mlx.core as mx

        if _nn is not None:
            _nn.Module.__init__(self)
        self.group_size, self.bits, self.pair = group_size, bits, pair
        self.dims = dims
        self.weight = mx.zeros(
            (packed_rows, packed_dim * bits // 32), dtype=mx.uint32)
        n_groups = packed_dim // group_size
        self.scales = mx.zeros((packed_rows, n_groups), dtype=mx.float16)
        self.biases = mx.zeros((packed_rows, n_groups), dtype=mx.float16)

    def __call__(self, idx):
        import mlx.core as mx
        idx = idx.astype(mx.int32)
        packed = idx // self.pair
        rows = mx.dequantize(self.weight[packed], self.scales[packed],
                             self.biases[packed],
                             group_size=self.group_size, bits=self.bits)
        off = (idx % self.pair) * self.dims
        cols = off[:, None] + mx.arange(self.dims)[None, :]
        return mx.take_along_axis(rows, cols, axis=1)

