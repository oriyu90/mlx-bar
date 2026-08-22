"""Runtime-agnostic prompt cache support shared by both workers.

Every decision here is made from a *capability* -- does this object expose
``trim``, does it expose ``state`` -- never from a model name, an architecture
string or a runtime version. A new architecture that MLXBar has never seen
therefore lands on the correct path on its own, and an upstream fix that adds a
missing method is picked up without a code change here.

Three things live in this module:

* **Capability probes.** Whether a cache can be rolled back to a shorter prefix
  (``trim``), and whether it can be captured and restored wholesale (``state``).
  Hybrid attention models -- Qwen3.5/3.8 and anything else mixing recurrent
  layers with full attention -- can do the second but not the first: a
  recurrent state has no notion of "drop the last N tokens".

* **Budget arithmetic.** How many bytes one token of cache costs for a given
  model, read out of the model's own ``config.json``. A fixed gigabyte limit
  cannot survive the next architecture; an arithmetic one can.

* **State capture.** Deep-copying and restoring the ``state`` trees, and
  flattening them for safetensors so a snapshot outlives the worker.

Nothing in here may raise into a generation. Every entry point returns a
"could not do it" value instead, because losing reuse costs time while failing
the request costs the answer.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

# Why a request could not reuse anything. Recorded so a silent degradation --
# the failure mode that cost days of confusion before v1.6.0 -- is visible in
# the API log and the UI instead of only in a warning nobody reads.
COLD_NO_PREFIX = "no_prefix"
COLD_REUSE_UNSUPPORTED = "reuse_unsupported"
COLD_BUDGET_INSUFFICIENT = "budget_insufficient"
COLD_CANCELLED_PREVIOUS = "cancelled_previous"
COLD_RUNTIME_CHANGED = "runtime_changed"
COLD_MEMORY_PRESSURE = "memory_pressure"
COLD_TOKEN_IDS_UNAVAILABLE = "token_ids_unavailable"
COLD_WRITE_BUDGET_REACHED = "write_budget_reached"
COLD_FIRST_REQUEST = "first_request"

COLD_REASONS = frozenset({
    COLD_NO_PREFIX, COLD_REUSE_UNSUPPORTED, COLD_BUDGET_INSUFFICIENT,
    COLD_CANCELLED_PREVIOUS, COLD_RUNTIME_CHANGED, COLD_MEMORY_PRESSURE,
    COLD_TOKEN_IDS_UNAVAILABLE, COLD_WRITE_BUDGET_REACHED, COLD_FIRST_REQUEST,
})

# How a retained cache can be moved back to an earlier prefix.
ROLLBACK_TRIM = "trim"            # the runtime can drop trailing tokens in place
ROLLBACK_CHECKPOINT = "checkpoint"  # only a captured copy can be restored
ROLLBACK_NONE = "none"            # neither; reuse is limited to exact continuation

DTYPE_BYTES = {
    "bfloat16": 2, "float16": 2, "half": 2, "bf16": 2, "fp16": 2,
    "float32": 4, "float": 4, "fp32": 4,
    "float64": 8, "int8": 1, "uint8": 1, "int32": 4, "uint32": 4,
}


def _dtype_bytes(name: Any, default: int = 2) -> int:
    key = str(name or "").strip().lower().replace("torch.", "")
    return DTYPE_BYTES.get(key, default)


# --------------------------------------------------------------- capability

def _entries(cache: Any) -> list:
    """Flatten a cache list, descending into composite caches.

    ``CacheList`` wraps several component caches and forwards ``trim`` to all of
    them, so the capability of the whole is the capability of the weakest part.
    """
    result: list = []
    for entry in cache or []:
        children = getattr(entry, "caches", None)
        if isinstance(children, (list, tuple)) and children:
            result.extend(_entries(children))
        else:
            result.append(entry)
    return result


def can_trim(cache: Any) -> bool:
    """Whether every component can drop trailing tokens in place.

    Both halves matter. A component may declare ``is_trimmable()`` and still be
    unable to help, and -- the case that broke Qwen3.5/3.8 hybrids -- a
    component may not define ``trim`` at all. mlx-vlm 0.6.15 guards its own
    rollback with a retention check that misses the second case, so MLXBar has
    to answer the question itself before the runtime reaches the call.
    """
    entries = _entries(cache)
    if not entries:
        return False
    for entry in entries:
        if not callable(getattr(entry, "trim", None)):
            return False
        probe = getattr(entry, "is_trimmable", None)
        if callable(probe):
            try:
                if not probe():
                    return False
            except Exception:
                return False
    return True


def can_capture(cache: Any) -> bool:
    """Whether every component exposes the ``state`` tree used for snapshots.

    This is the same contract mlx-lm's ``save_prompt_cache`` relies on, so it is
    as stable as cache serialisation itself. A recurrent component satisfies it
    even though it can never satisfy :func:`can_trim`.
    """
    entries = _entries(cache)
    if not entries:
        return False
    for entry in entries:
        if not hasattr(entry, "state"):
            return False
        setter = getattr(type(entry), "state", None)
        if not isinstance(setter, property) or setter.fset is None:
            return False
    return True


def rollback_capability(cache: Any) -> str:
    if can_trim(cache):
        return ROLLBACK_TRIM
    if can_capture(cache):
        return ROLLBACK_CHECKPOINT
    return ROLLBACK_NONE


def cached_length(cache: Any) -> int | None:
    """Tokens currently held, or None when no component reports an offset.

    Used to verify -- after the fact -- that a cache really did advance during a
    generation before its token ids are rewritten. Without a reported offset
    there is nothing to verify against, and the caller must not guess.
    """
    offsets = [int(getattr(entry, "offset", -1) or 0)
               for entry in _entries(cache) if getattr(entry, "offset", None) is not None]
    return max(offsets) if offsets else None


# ------------------------------------------------------------------ budget

def _text_config(config: dict) -> dict:
    inner = config.get("text_config")
    if isinstance(inner, dict) and inner:
        merged = dict(config)
        merged.update(inner)
        return merged
    return dict(config)


def model_cache_budget(config: dict) -> dict:
    """Bytes of cache this model needs, derived from its own config.

    Returns ``perTokenBytes`` (full-attention layers, which grow with the
    conversation), ``fixedBytes`` (recurrent layers, which do not) and
    ``known``. When the fields cannot be read, ``known`` is False and callers
    must keep their previous behaviour rather than act on a guess -- an
    invented number here would silently disable a cache that works.
    """
    unknown = {"known": False, "perTokenBytes": 0, "fixedBytes": 0,
               "fullAttentionLayers": 0, "recurrentLayers": 0}
    if not isinstance(config, dict):
        return unknown
    text = _text_config(config)
    try:
        layers = int(text.get("num_hidden_layers") or 0)
    except (TypeError, ValueError):
        return unknown
    if layers <= 0:
        return unknown

    layer_types = text.get("layer_types")
    if isinstance(layer_types, (list, tuple)) and layer_types:
        full = sum(1 for item in layer_types if "full" in str(item).lower())
        recurrent = len(layer_types) - full
    else:
        interval = text.get("full_attention_interval")
        try:
            interval = int(interval) if interval else 0
        except (TypeError, ValueError):
            interval = 0
        if interval > 1:
            full = layers // interval
            recurrent = layers - full
        else:
            full, recurrent = layers, 0

    dtype_bytes = _dtype_bytes(text.get("dtype") or text.get("torch_dtype"))
    try:
        kv_heads = int(text.get("num_key_value_heads")
                       or text.get("num_attention_heads") or 0)
    except (TypeError, ValueError):
        return unknown
    head_dim = text.get("head_dim")
    try:
        head_dim = int(head_dim) if head_dim else 0
        if head_dim <= 0:
            hidden = int(text.get("hidden_size") or 0)
            heads = int(text.get("num_attention_heads") or 0)
            head_dim = hidden // heads if hidden and heads else 0
    except (TypeError, ValueError, ZeroDivisionError):
        return unknown
    if kv_heads <= 0 or head_dim <= 0:
        return unknown

    # Keys and values, per full-attention layer, per token.
    per_token = full * 2 * kv_heads * head_dim * dtype_bytes

    fixed = 0
    if recurrent:
        ssm_bytes = _dtype_bytes(text.get("mamba_ssm_dtype"), default=dtype_bytes)
        try:
            value_heads = int(text.get("linear_num_value_heads") or 0)
            key_heads = int(text.get("linear_num_key_heads") or 0)
            key_dim = int(text.get("linear_key_head_dim") or 0)
            value_dim = int(text.get("linear_value_head_dim") or 0)
            conv_kernel = int(text.get("linear_conv_kernel_dim") or 0)
        except (TypeError, ValueError):
            value_heads = key_heads = key_dim = value_dim = conv_kernel = 0
        if value_heads and key_dim and value_dim:
            recurrent_state = value_heads * key_dim * value_dim * ssm_bytes
            conv_width = (key_heads * key_dim * 2) + (value_heads * value_dim)
            conv_state = conv_width * max(0, conv_kernel - 1) * dtype_bytes
            fixed = recurrent * (recurrent_state + conv_state)
        # A recurrent layer whose geometry cannot be read contributes nothing to
        # the estimate. The per-token term still dominates, so the snapshot size
        # stays usable; it is an under-estimate, never an over-estimate.

    return {"known": True, "perTokenBytes": int(per_token), "fixedBytes": int(fixed),
            "fullAttentionLayers": int(full), "recurrentLayers": int(recurrent)}


def snapshot_bytes(budget: dict, tokens: int) -> int:
    """Bytes one snapshot of ``tokens`` tokens would occupy."""
    if not budget or not budget.get("known"):
        return 0
    return int(budget.get("fixedBytes", 0)) + int(budget.get("perTokenBytes", 0)) * max(0, int(tokens))


def affordable_tokens(budget: dict, byte_limit: int) -> int:
    """Longest snapshot that fits in ``byte_limit``. 0 when even the fixed part does not."""
    if not budget or not budget.get("known") or byte_limit <= 0:
        return 0
    per_token = int(budget.get("perTokenBytes", 0))
    remaining = int(byte_limit) - int(budget.get("fixedBytes", 0))
    if remaining <= 0 or per_token <= 0:
        return 0
    return remaining // per_token


# ------------------------------------------------------------ state capture

def _copy_array(value):
    import mlx.core as mx
    return mx.contiguous(mx.array(value, dtype=value.dtype))


def _map_tree(node, on_array):
    import mlx.core as mx
    if isinstance(node, mx.array):
        return on_array(node)
    if isinstance(node, tuple):
        return tuple(_map_tree(item, on_array) for item in node)
    if isinstance(node, list):
        return [_map_tree(item, on_array) for item in node]
    if isinstance(node, dict):
        return {key: _map_tree(item, on_array) for key, item in node.items()}
    return node


def capture(cache: Any) -> list | None:
    """Deep-copy every component's state, or None when any component refuses.

    The copy has to be a real one: the runtime advances the live cache in place
    during generation, so a shared buffer would follow it forward and stop being
    a snapshot of anything.
    """
    entries = _entries(cache)
    if not entries or not can_capture(cache):
        return None
    payload = []
    try:
        for entry in entries:
            payload.append({"state": _map_tree(entry.state, _copy_array),
                            "meta": getattr(entry, "meta_state", None)})
    except Exception as exc:
        LOGGER.warning("Prompt cache capture failed; continuing without it: %s", exc)
        return None
    return payload


def restore(cache: Any, payload: list) -> bool:
    """Write a captured payload back into freshly made cache objects.

    ``cache`` must come from the runtime's own ``make_prompt_cache`` so the
    component classes and their order match the model. Restoring copies again:
    the caller keeps its snapshot for the next turn, and generation must not
    advance it.
    """
    entries = _entries(cache)
    if not payload or len(entries) != len(payload):
        return False
    try:
        for entry, item in zip(entries, payload):
            entry.state = _map_tree(item["state"], _copy_array)
            meta = item.get("meta")
            if meta is not None and hasattr(entry, "meta_state"):
                entry.meta_state = meta
    except Exception as exc:
        LOGGER.warning("Prompt cache restore failed; falling back to cold prefill: %s", exc)
        return False
    return True


def payload_bytes(payload: list) -> int:
    total = 0

    def measure(value):
        nonlocal total
        total += int(getattr(value, "nbytes", 0) or 0)
        return value

    for item in payload or []:
        _map_tree(item.get("state"), measure)
    return total


def evaluate(payload: list) -> None:
    """Force the copies to be materialised before they are counted or written."""
    import mlx.core as mx
    targets: list = []

    def collect(value):
        targets.append(value)
        return value

    for item in payload or []:
        _map_tree(item.get("state"), collect)
    if targets:
        mx.eval(*targets)


# -------------------------------------------------------------- serialising

def flatten(payload: list) -> tuple[dict, list]:
    """Split a payload into safetensors-writable arrays plus a shape manifest.

    safetensors stores a flat name->array map, so the tree structure (lists,
    tuples, ``None`` holes, plain scalars) travels beside it as JSON. Without
    the manifest a restored ``ArraysCache`` would lose the ``None`` entries that
    mark layers which have not run yet.
    """
    import mlx.core as mx
    arrays: dict = {}
    counter = [0]

    def describe(node):
        if isinstance(node, mx.array):
            name = f"a{counter[0]}"
            counter[0] += 1
            arrays[name] = node
            return {"t": "a", "k": name}
        if isinstance(node, tuple):
            return {"t": "t", "c": [describe(item) for item in node]}
        if isinstance(node, list):
            return {"t": "l", "c": [describe(item) for item in node]}
        if isinstance(node, dict):
            return {"t": "d", "c": {key: describe(item) for key, item in node.items()}}
        if node is None:
            return {"t": "n"}
        if isinstance(node, (int, float, bool, str)):
            return {"t": "v", "v": node}
        # Anything else cannot be persisted faithfully; refuse the whole
        # snapshot rather than write one that restores into something subtly
        # different.
        raise TypeError(f"unsupported cache state node: {type(node).__name__}")

    manifest = [{"state": describe(item.get("state")), "meta": item.get("meta")}
                for item in payload or []]
    return arrays, manifest


def unflatten(arrays: dict, manifest: list) -> list:
    def build(node):
        kind = node.get("t")
        if kind == "a":
            return arrays[node["k"]]
        if kind == "t":
            return tuple(build(item) for item in node.get("c", []))
        if kind == "l":
            return [build(item) for item in node.get("c", [])]
        if kind == "d":
            return {key: build(item) for key, item in (node.get("c") or {}).items()}
        if kind == "v":
            return node.get("v")
        return None

    def meta(value):
        # JSON has no tuples, and the runtime's `meta_state` is one. Restoring a
        # list where a tuple was captured is the kind of difference that only
        # shows up as a confusing failure several layers down.
        return tuple(value) if isinstance(value, list) else value

    return [{"state": build(item.get("state", {})), "meta": meta(item.get("meta"))}
            for item in manifest or []]
