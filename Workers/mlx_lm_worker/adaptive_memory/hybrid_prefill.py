"""Hybrid Prefiller -- prefill EXACT-token and LATENT-soft-token blocks into
one KV cache, in order, then hand the cache + un-prefilled tail to
``stream_generate`` (DESIGN_v2.2.0.md §6).

    exact head  -> model(tokens, cache)
    latent band -> model(placeholder, cache, input_embeddings=soft)
    (exact tail -> left for stream_generate as `prompt`)

Raises ``PrefillError`` on anything unexpected; the runtime converts that to
``FallbackToExact``.
"""

from __future__ import annotations

import inspect


class PrefillError(RuntimeError):
    pass


def accepts_input_embeddings(model) -> bool:
    for attr in ("__call__", "forward"):
        target = getattr(model, attr, None)
        if target is None:
            continue
        try:
            if "input_embeddings" in inspect.signature(target).parameters:
                return True
        except (TypeError, ValueError):
            continue
    return False


def prefill(model, plan: list[dict]):
    """``plan`` items: {"kind":"exact","tokens":[...]} | {"kind":"latent","soft":mx.array}.

    Returns the populated prompt cache.
    """
    import mlx.core as mx
    try:
        from mlx_lm.models.cache import make_prompt_cache
    except Exception as exc:  # pragma: no cover - mlx-lm shape
        raise PrefillError(f"make_prompt_cache unavailable: {exc}") from exc

    cache = make_prompt_cache(model)
    last = None
    for block in plan:
        if block["kind"] == "exact":
            tokens = block["tokens"]
            if not tokens:
                continue
            x = mx.array(tokens, dtype=mx.uint32)[None]
            last = model(x, cache=cache)
        else:
            soft = block["soft"]
            if soft.ndim != 2:
                raise PrefillError(f"soft token array must be 2-D, got {soft.shape}")
            emb = soft[None]
            placeholder = mx.zeros((1, emb.shape[1]), dtype=mx.uint32)
            try:
                last = model(placeholder, cache=cache, input_embeddings=emb)
            except TypeError as exc:
                raise PrefillError(f"reader rejected input_embeddings: {exc}") from exc
        mx.eval(last)
    try:
        mx.eval([layer.state for layer in cache])
    except Exception:  # noqa: BLE001 - not all cache types expose .state
        if last is not None:
            mx.eval(last)
    return cache
