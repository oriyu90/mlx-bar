"""``AdaptiveRuntime`` -- orchestrates the soft-token Hybrid Prefiller.

``prepare(prompt_tokens)`` is the single entry point the adapter calls. It
returns a plan dict on success and raises ``FallbackToExact`` -- and nothing
else -- on any doubt. Nothing is generated here; the adapter feeds the
returned cache + tail to ``stream_generate``.
"""

from __future__ import annotations

import logging
import time

from . import hybrid_prefill, policy, writer, writer_registry
from .hybrid_cache import HybridKVCache, block_hash, hybrid_key
from .latent_store import LatentCache, latent_key
from .metrics import HybridMetrics

LOGGER = logging.getLogger(__name__)


class FallbackToExact(Exception):
    """Signal: abandon the adaptive path, run the ordinary EXACT path."""


def _mlx_lm_version() -> str:
    try:
        import importlib.metadata
        return importlib.metadata.version("mlx-lm")
    except Exception:  # noqa: BLE001
        return "unknown"


class AdaptiveRuntime:
    def __init__(self, model, processor, config: dict, *, cache_root: str | None = None):
        self.model = model
        self.processor = processor
        self.config = config or {}
        self.cache_root = cache_root
        self.policy = str(self.config.get("policy", "balanced"))
        self.max_latent = int(self.config.get("maxLatentTokens", 256))
        self.policy_version = str(self.config.get("policyVersion", "am1"))
        self.persist_latent = bool(self.config.get("persistentLatentCache", True))
        self.persist_hybrid = bool(self.config.get("persistentHybridKV", True))

    def should_activate(self) -> bool:
        return bool(self.config.get("enabled") and self.config.get("softToken"))

    def prepare(self, prompt_tokens: list[int]) -> dict:
        try:
            return self._prepare(prompt_tokens)
        except FallbackToExact:
            raise
        except Exception as exc:  # noqa: BLE001 - never let anything else escape
            raise FallbackToExact(f"{type(exc).__name__}: {exc}") from exc

    def _prepare(self, prompt_tokens: list[int]) -> dict:
        if not self.should_activate():
            raise FallbackToExact("softToken disabled")
        if not isinstance(prompt_tokens, list) or len(prompt_tokens) < policy.MIN_BAND_TOKENS * 2:
            raise FallbackToExact("prompt too short for a soft-token band")
        if not hybrid_prefill.accepts_input_embeddings(self.model):
            raise FallbackToExact("reader does not accept input_embeddings")

        metrics = HybridMetrics(raw_prompt_tokens=len(prompt_tokens), writer_id=writer.WRITER_ID)

        policy_started = time.monotonic()
        banding = policy.band(len(prompt_tokens), self.policy)
        metrics.policy_ms = round((time.monotonic() - policy_started) * 1000, 2)
        if banding is None:
            raise FallbackToExact("banding produced no compressible middle")

        head = prompt_tokens[: banding.head]
        band = prompt_tokens[banding.band_start: banding.band_end]
        tail = prompt_tokens[banding.band_end:]

        soft_fn, fp_fn = writer_registry.get(writer_registry.DEFAULT_WRITER_ID)
        writer_fp = fp_fn(self.max_latent)
        reader_fp = f"{type(self.model).__name__}"

        # --- Writer (with content-addressed latent cache) ---
        writer_started = time.monotonic()
        try:
            probe_hidden = self._hidden_size()
        except writer.WriterUnavailable as exc:
            raise FallbackToExact(str(exc))
        latent_cache = LatentCache(self.cache_root if self.persist_latent else None, probe_hidden)
        key = latent_key(band, reader_fp, writer_fp, self.policy_version)
        soft = latent_cache.get(key)
        if soft is None:
            try:
                soft = soft_fn(self.model, band, self.max_latent)
            except writer.WriterUnavailable as exc:
                raise FallbackToExact(str(exc))
            latent_cache.put(key, soft)
        else:
            metrics.latent_cache_hit = True
        metrics.writer_ms = round((time.monotonic() - writer_started) * 1000, 2)

        if soft.ndim != 2 or soft.shape[0] < 1 or soft.shape[1] != probe_hidden:
            raise FallbackToExact(f"soft token shape {tuple(soft.shape)} inconsistent with hidden {probe_hidden}")

        # Hybrid KV cache key is composed now (reuse is a future version).
        hybrid = HybridKVCache(self.persist_hybrid)
        hk = hybrid_key(reader_fp=reader_fp, writer_fp=writer_fp,
                        template_fp=self._template_fingerprint(),
                        policy_version=self.policy_version, mlx_lm_version=_mlx_lm_version(),
                        exact_block_hash=block_hash(head), latent_block_hash=block_hash(band))
        cache = hybrid.get(hk)

        # --- Hybrid prefill ---
        prefill_started = time.monotonic()
        if cache is None:
            try:
                cache = hybrid_prefill.prefill(self.model, [
                    {"kind": "exact", "tokens": head},
                    {"kind": "latent", "soft": soft},
                ])
            except hybrid_prefill.PrefillError as exc:
                raise FallbackToExact(str(exc))
            hybrid.put(hk, cache)
        else:
            metrics.hybrid_kv_hit = True
        metrics.prefill_ms = round((time.monotonic() - prefill_started) * 1000, 2)

        latent_n = int(soft.shape[0])
        effective = len(head) + latent_n + len(tail)
        metrics.effective_prompt_tokens = effective
        metrics.exact_tokens = len(head) + len(tail)
        metrics.latent_tokens = latent_n
        metrics.compression_ratio = round(effective / max(1, len(prompt_tokens)), 4)

        LOGGER.info("adaptive soft-token: %d -> %d tokens (band %d -> %d), writer %.0fms prefill %.0fms",
                    len(prompt_tokens), effective, len(band), latent_n,
                    metrics.writer_ms, metrics.prefill_ms)
        return {"cache": cache, "remaining": list(tail), "all_tokens": list(prompt_tokens),
                "metrics": metrics.as_event()}

    def _hidden_size(self) -> int:
        import mlx.core as mx
        embed = writer._embed_layer(self.model)
        probe = embed(mx.array([0], dtype=mx.uint32))
        if probe.ndim != 2:
            raise writer.WriterUnavailable(f"embedding probe shape {probe.shape}")
        return int(probe.shape[1])

    def _template_fingerprint(self) -> str:
        template = getattr(self.processor, "chat_template", None)
        if not isinstance(template, str):
            return "none"
        import hashlib
        return hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]
