"""Opt-in, fail-closed paged KV cache for plain mlx-lm KVCache layers."""

from .store import PagedKVStore

__all__ = ["PagedKVStore"]
