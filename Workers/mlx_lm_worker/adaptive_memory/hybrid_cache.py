"""Hybrid KV Cache store (DESIGN_v2.2.0.md §7).

The key composition below is the contract: a hybrid KV cache is only reusable
when the reader, Writer, chat template, policy version, mlx-lm version and both
block hashes all match. v1 does **not** persist or reuse hybrid KV state --
``get`` always misses and ``put`` is a no-op -- because a partially
soft-token-prefilled cache has no measured round-trip on a 27B-class hybrid
yet (mlx-bar.md: "実測がないなら既定値を動かさない"). The seam and the key are
in place for a later version to fill in.
"""

from __future__ import annotations

import hashlib


def hybrid_key(*, reader_fp: str, writer_fp: str, template_fp: str, policy_version: str,
               mlx_lm_version: str, exact_block_hash: str, latent_block_hash: str) -> str:
    digest = hashlib.sha256()
    for part in (reader_fp, writer_fp, template_fp, policy_version, mlx_lm_version,
                 exact_block_hash, latent_block_hash):
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def block_hash(tokens: list[int]) -> str:
    import array
    return hashlib.sha256(bytes(memoryview(
        array.array("I", (t & 0xFFFFFFFF for t in tokens))))).hexdigest()


class HybridKVCache:
    def __init__(self, persistent: bool):
        self.persistent = persistent  # honoured by a future version

    def get(self, key: str):
        return None

    def put(self, key: str, cache) -> None:
        return None
