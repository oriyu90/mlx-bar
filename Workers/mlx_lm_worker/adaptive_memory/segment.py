"""Token-level segmentation of the rendered prompt.

The coordinator tier owns semantic segmentation; this module only turns a
``Banding`` (from ``policy.band``) plus the full token list into the ordered
block plan the Hybrid Prefiller consumes: an EXACT head, one LATENT band, and
an EXACT tail.
"""

from __future__ import annotations

from dataclasses import dataclass

from .policy import Banding


@dataclass(frozen=True)
class Block:
    kind: str          # "exact" | "latent"
    tokens: list[int]   # exact: the tokens; latent: the source tokens to compress


def plan_blocks(tokens: list[int], banding: Banding) -> list[Block]:
    head = tokens[: banding.head]
    band = tokens[banding.band_start: banding.band_end]
    # Tail is handed to stream_generate as the un-prefilled prompt, so it is
    # not a block here; the caller keeps it separately.
    return [Block("exact", head), Block("latent", band)]


def tail_tokens(tokens: list[int], banding: Banding) -> list[int]:
    return tokens[banding.band_end:]
