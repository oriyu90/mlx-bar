from __future__ import annotations

import hashlib

from .types import CacheCapability


def probe_plain_kv(cache) -> CacheCapability:
    """Accept only the runtime's exact, unquantized KVCache implementation."""
    try:
        from mlx_lm.models.cache import KVCache
    except (ImportError, AttributeError):
        return CacheCapability(False, "unsupported", "kv_cache_class_unavailable", None, 0, 0)
    if not isinstance(cache, (list, tuple)) or not cache:
        return CacheCapability(False, "unsupported", "empty_cache_layout", None, 0, 0)
    if any(type(layer) is not KVCache for layer in cache):
        return CacheCapability(False, "checkpoint_only", "non_plain_kv_layout", None, len(cache), 0)
    descriptor = getattr(KVCache, "state", None)
    if not isinstance(descriptor, property) or descriptor.fset is None:
        return CacheCapability(False, "unsupported", "state_setter_unavailable", None, len(cache), 0)
    # mlx-lm 0.31.x serializes (keys, values); newer runtimes include offset as
    # a third state item. Exercise the actual setter with a tiny real MLX array
    # instead of inferring from a version or reading the empty state getter.
    try:
        import mlx.core as mx
        tiny = mx.zeros((1, 1, 1, 1))
        state_arity = 0
        for candidate in (3, 2):
            probe = KVCache()
            try:
                probe.state = ((tiny, tiny, 1) if candidate == 3 else (tiny, tiny))
                if int(getattr(probe, "offset", -1)) == 1:
                    state_arity = candidate
                    break
            except Exception:
                continue
        if state_arity not in {2, 3}:
            raise ValueError("unsupported state arity")
    except Exception:
        return CacheCapability(False, "unsupported", "state_round_trip_failed", None, len(cache), 0)
    qualified = [f"{type(layer).__module__}.{type(layer).__qualname__}" for layer in cache]
    digest = hashlib.sha256(("\0".join(qualified) + f"\0state:{state_arity}\0axis:2").encode()).hexdigest()
    return CacheCapability(True, "block", None, digest, len(cache), state_arity)


def live_layout(cache, expected_layers: int) -> tuple[list[dict], int]:
    """Describe a populated plain cache without retaining its tensors."""
    capability = probe_plain_kv(cache)
    if not capability.eligible or len(cache) != expected_layers:
        raise ValueError(capability.reason or "layer_count_mismatch")
    layout = []
    offsets = set()
    for layer in cache:
        state = layer.state
        if not isinstance(state, (list, tuple)) or len(state) != capability.state_arity:
            raise ValueError("state_arity_changed")
        keys, values = state[:2]
        offset = getattr(layer, "offset", state[2] if len(state) == 3 else None)
        if keys is None or values is None:
            raise ValueError("empty_layer")
        offset = int(offset)
        offsets.add(offset)
        if len(keys.shape) != 4 or len(values.shape) != 4:
            raise ValueError("state_rank_mismatch")
        if int(keys.shape[2]) < offset or int(values.shape[2]) < offset:
            raise ValueError("offset_outside_state")
        layout.append({
            "keyShape": [int(keys.shape[0]), int(keys.shape[1]), int(keys.shape[3])],
            "valueShape": [int(values.shape[0]), int(values.shape[1]), int(values.shape[3])],
            "keyDtype": str(keys.dtype), "valueDtype": str(values.dtype),
        })
    if len(offsets) != 1:
        raise ValueError("layer_offsets_disagree")
    return layout, offsets.pop()
