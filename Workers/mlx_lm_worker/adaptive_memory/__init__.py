"""Soft-token Hybrid Prefiller for the mlx-lm worker -- experimental.

Second tier of Adaptive Hybrid Context Memory (DESIGN_v2.2.0.md). The
coordinator tier has already dropped COLD turns and condensed LATENT
discussion to notes; when ``experimental.adaptiveMemory.softToken`` is on this
tier goes further, compressing an *older band* of the rendered prompt's tokens
into a handful of soft tokens (mean-pooled embeddings from the model's own
input embedding layer) and prefilling them into the KV cache ahead of the
verbatim recent tail -- the doc's "Static Hybrid": old -> LATENT, recent ->
EXACT.

Everything here fails closed. ``AdaptiveRuntime.prepare`` raises
``FallbackToExact`` -- never anything else -- when the reader cannot take
``input_embeddings``, an embedding layer cannot be found, dimensions do not
line up, or any step errors. The adapter catches that and runs the ordinary
EXACT path, so the experiment can never turn into a failed request or a
crashed worker.
"""

from __future__ import annotations

from .runtime import AdaptiveRuntime, FallbackToExact

__all__ = ["AdaptiveRuntime", "FallbackToExact"]
