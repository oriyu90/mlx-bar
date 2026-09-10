"""Writer ``meanpool-v1`` -- text tokens -> soft tokens, no training.

The design doc's Writer is a learned compressor. v1 ships the simplest thing
that exercises the whole Hybrid Prefiller mechanism end to end without a
trained checkpoint: embed the band's tokens through the model's *own* input
embedding layer, then mean-pool contiguous windows down to at most
``max_latent`` vectors. It is deterministic and parameter-free.

Quality is not claimed -- this is a mechanism to measure on real hardware, not
a fidelity feature, which is why ``softToken`` ships OFF. Anything unexpected
about the embedding layer raises so the caller can fall back to EXACT.
"""

from __future__ import annotations

WRITER_ID = "meanpool-v1"


class WriterUnavailable(RuntimeError):
    """The model exposes no usable input embedding layer."""


def _embed_layer(model):
    for path in (("model", "embed_tokens"), ("embed_tokens",),
                 ("model", "model", "embed_tokens"), ("transformer", "wte")):
        node = model
        try:
            for name in path:
                node = getattr(node, name)
        except AttributeError:
            continue
        if callable(node):
            return node
    raise WriterUnavailable("no input embedding layer found on the model")


def soft_tokens(model, token_ids: list[int], max_latent: int):
    """Return an ``mx.array`` of shape ``[n_latent, hidden]`` (n_latent <= max_latent)."""
    import mlx.core as mx

    if not token_ids:
        raise WriterUnavailable("empty band")
    embed = _embed_layer(model)
    ids = mx.array(token_ids, dtype=mx.uint32)
    hidden = embed(ids)  # [n, hidden]
    if hidden.ndim != 2 or hidden.shape[0] != len(token_ids):
        raise WriterUnavailable(f"unexpected embedding shape {hidden.shape}")
    n = hidden.shape[0]
    windows = max(1, min(int(max_latent), n))
    # Even split into `windows` contiguous groups; last group absorbs the
    # remainder. Pool with mean so scale stays comparable to a real embedding.
    size = -(-n // windows)  # ceil
    pooled = [hidden[i:i + size].mean(axis=0) for i in range(0, n, size)]
    out = mx.stack(pooled, axis=0).astype(hidden.dtype)
    mx.eval(out)
    return out


def fingerprint(max_latent: int) -> str:
    return f"{WRITER_ID}:ml{int(max_latent)}"
