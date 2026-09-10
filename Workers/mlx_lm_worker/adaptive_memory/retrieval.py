"""Retriever v1.

Static Hybrid banding produces a single LATENT band, so retrieval is a no-op
pass-through today: the whole band is compressed and included. The seam is
kept so a later Adaptive policy can split the band into segments and select a
``maxRetrievedSegments`` subset, leaving the rest COLD.
"""

from __future__ import annotations

from .segment import Block


def select(blocks: list[Block], max_retrieved_segments: int) -> list[Block]:
    latent = [b for b in blocks if b.kind == "latent"]
    if len(latent) <= max(1, int(max_retrieved_segments)):
        return blocks
    keep = set(id(b) for b in latent[-int(max_retrieved_segments):])
    return [b for b in blocks if b.kind != "latent" or id(b) in keep]
