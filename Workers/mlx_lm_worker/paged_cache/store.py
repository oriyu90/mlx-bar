from __future__ import annotations

import importlib.metadata
import json
import logging
import os
from pathlib import Path

from .capabilities import live_layout, probe_plain_kv
from .disk import DiskBlockStore
from .hashing import chain, namespace_fingerprint, token_digest
from .types import (BLOCK_SIZE, FORMAT_VERSION, MIN_REUSABLE_TOKENS, CorruptBlock,
                    LayoutMismatch, RestoreBudgetRejected)

LOGGER = logging.getLogger(__name__)


class PagedKVStore:
    """Content-addressed SSD blocks for plain mlx-lm KVCache only."""

    def __init__(self, *, model, model_fingerprint: str, runtime_version: str,
                 root: str, max_bytes: int, disk_enabled: bool, branch_reuse: bool,
                 cache_budget: dict):
        self.configured = True
        self.disk_enabled = bool(disk_enabled)
        self.branch_reuse = bool(branch_reuse)
        self.cache_budget = dict(cache_budget or {})
        self.hits = self.misses = self.tokens_restored = self.stores = 0
        self.deduplicated_stores = self.restore_budget_rejects = 0
        self.store_budget_rejects = 0
        self.corrupt_blocks = self.exact_fallbacks = 0
        self.last_miss_reason = self.last_fallback_reason = None
        self.consecutive_restore_failures = 0
        self.circuit_open = False
        self.disk: DiskBlockStore | None = None
        try:
            from mlx_lm.models.cache import make_prompt_cache
            empty = make_prompt_cache(model)
            self.capability = probe_plain_kv(empty)
        except Exception as exc:
            self.capability = probe_plain_kv(None)
            self.capability = type(self.capability)(False, "unsupported",
                                                    f"probe_failed:{type(exc).__name__}", None, 0, 0)
        try:
            mlx_version = importlib.metadata.version("mlx")
        except Exception:
            mlx_version = "unknown"
        self.namespace = namespace_fingerprint(
            model_fingerprint, runtime_version, mlx_version,
            ([f"mlx_lm.models.cache.KVCache:state{self.capability.state_arity}"]
             * self.capability.layers))
        self.semantic_salt = self.namespace
        if (self.disk_enabled and self.capability.eligible and root
                and self.cache_budget.get("known")):
            try:
                self.disk = DiskBlockStore(Path(root), self.namespace, max_bytes)
            except Exception as exc:
                self.capability = type(self.capability)(False, "unsupported",
                                                        f"disk_init_failed:{type(exc).__name__}",
                                                        self.capability.layout_fingerprint,
                                                        self.capability.layers,
                                                        self.capability.state_arity)

    def fetch(self, model, tokens: list[int]):
        if not self._available():
            self._miss(self._unavailable_reason())
            return None
        descriptors = chain(tokens[:-1], self.semantic_salt)
        if not descriptors:
            self._miss("no_full_block")
            return None
        available = []
        for descriptor in descriptors:
            if not self.disk.exists(descriptor["hash"]):
                break
            available.append(descriptor)
        exact = (self.disk.exact_matches(token_digest(tokens), descriptors[-1]["hash"],
                                         len(tokens), len(descriptors))
                 if available and not self.branch_reuse else True)
        if (not available
                or (not self.branch_reuse and len(available) != len(descriptors))
                or not exact):
            self._miss("no_prefix" if self.branch_reuse else "branch_reuse_disabled")
            return None
        length = len(available) * BLOCK_SIZE
        try:
            self._admit_restore(length)
            cache, length = self._restore(model, available)
            if not self.branch_reuse and length != len(descriptors) * BLOCK_SIZE:
                raise CorruptBlock("exact_restore_became_partial")
        except RestoreBudgetRejected:
            self.restore_budget_rejects += 1
            self._miss("restore_budget_rejected")
            return None
        except LayoutMismatch as exc:
            self._restore_failed("layout_mismatch", str(exc), immediate=True)
            return None
        except Exception as exc:
            self._restore_failed("restore_failed", str(exc))
            return None
        self.hits += 1
        self.tokens_restored += length
        self.consecutive_restore_failures = 0
        self.last_miss_reason = None
        return cache, length

    def store(self, tokens: list[int], cache, prompt_length: int) -> None:
        if not self._available() or cache is None:
            return
        # Always leave one raw token for mlx-lm's first forward pass.
        block_count = max(0, (min(len(tokens), int(prompt_length)) - 1) // BLOCK_SIZE)
        if block_count <= 0:
            return
        try:
            try:
                layout, offset = live_layout(cache, self.capability.layers)
            except ValueError as exc:
                raise LayoutMismatch(str(exc)) from exc
            if offset != len(tokens) or offset < prompt_length:
                raise LayoutMismatch("live_cache_offset_mismatch")
            descriptors = chain(tokens[:block_count * BLOCK_SIZE], self.semantic_salt)
            for descriptor in descriptors:
                if self.disk.exists(descriptor["hash"]):
                    self.deduplicated_stores += 1
                    continue
                start = descriptor["start"]
                expected_bytes = int(self.cache_budget.get("perTokenBytes", 0)) * BLOCK_SIZE
                try:
                    self._admit_bytes(expected_bytes)
                except RestoreBudgetRejected:
                    self.store_budget_rejects += 1
                    self._miss("store_budget_rejected")
                    return
                arrays = {}
                for index, layer in enumerate(cache):
                    state = layer.state
                    keys, values = state[:2]
                    arrays[f"k_{index:04d}"] = keys[..., start:start + BLOCK_SIZE, :]
                    arrays[f"v_{index:04d}"] = values[..., start:start + BLOCK_SIZE, :]
                import mlx.core as mx
                arrays = {name: mx.contiguous(value) for name, value in arrays.items()}
                mx.eval(*arrays.values())
                block_bytes = sum(int(value.nbytes) for value in arrays.values())
                if block_bytes != expected_bytes:
                    raise LayoutMismatch("block_size_disagrees_with_model_budget")
                if self.disk.max_bytes <= 0 or block_bytes > self.disk.max_bytes:
                    self._miss("block_too_large")
                    return
                metadata = {
                    "format": str(FORMAT_VERSION), "namespace": self.namespace,
                    "hash": descriptor["hash"], "parent": descriptor["parent"],
                    "tokenDigest": descriptor["tokens"], "start": str(start),
                    "tokens": str(BLOCK_SIZE), "layout": layout,
                    "layoutFingerprint": self.capability.layout_fingerprint or "",
                }
                if self.disk.save(descriptor["hash"], arrays, metadata):
                    self.stores += 1
                else:
                    self.deduplicated_stores += 1
                del arrays
                # Enforce after each immutable block. Peak disk usage is thus
                # bounded by quota plus the one same-directory temporary.
                self.disk.prune()
            if all(self.disk.exists(item["hash"]) for item in descriptors):
                self.disk.mark_exact(token_digest(tokens[:prompt_length]), descriptors[-1]["hash"],
                                     prompt_length, len(descriptors))
        except LayoutMismatch as exc:
            self._restore_failed("store_layout_mismatch", str(exc), immediate=True)
        except Exception as exc:
            LOGGER.warning("Paged KV store skipped: %s", exc)

    def mark_exact_fallback(self, reason: str) -> None:
        self.exact_fallbacks += 1
        self.last_fallback_reason = reason
        self.consecutive_restore_failures += 1
        if self.consecutive_restore_failures >= 3:
            self.circuit_open = True

    def clear(self) -> None:
        if self.disk is not None:
            self.disk.clear()
        self.hits = self.misses = self.tokens_restored = self.stores = 0
        self.deduplicated_stores = self.restore_budget_rejects = 0
        self.store_budget_rejects = 0
        self.corrupt_blocks = self.exact_fallbacks = 0
        self.last_miss_reason = self.last_fallback_reason = None
        self.consecutive_restore_failures = 0
        self.circuit_open = False

    def stats(self) -> dict:
        blocks, disk_bytes = self.disk.stats() if self.disk is not None else (0, 0)
        disabled_reason = self.capability.reason
        if self.circuit_open:
            disabled_reason = "circuit_open"
        elif not self.disk_enabled:
            disabled_reason = "disk_disabled"
        elif not self.cache_budget.get("known"):
            disabled_reason = "budget_unknown"
        elif self.disk is None and disabled_reason is None:
            disabled_reason = "disk_unavailable"
        return {
            "configured": True, "enabled": self.disk is not None and not self.circuit_open,
            "eligible": self.capability.eligible, "capability": self.capability.capability,
            "disabledReason": disabled_reason,
            "layoutFingerprint": self.capability.layout_fingerprint,
            "formatVersion": FORMAT_VERSION, "blockSize": BLOCK_SIZE,
            "diskBytes": disk_bytes, "diskBlocks": blocks,
            "hits": self.hits, "misses": self.misses, "tokensRestored": self.tokens_restored,
            "stores": self.stores, "deduplicatedStores": self.deduplicated_stores,
            "restoreBudgetRejects": self.restore_budget_rejects,
            "storeBudgetRejects": self.store_budget_rejects,
            "corruptBlocks": self.corrupt_blocks, "exactFallbacks": self.exact_fallbacks,
            "lastMissReason": self.last_miss_reason,
            "lastFallbackReason": self.last_fallback_reason,
            "branchReuse": self.branch_reuse, "memoryTier": "off",
            "circuitOpen": self.circuit_open,
        }

    def _available(self) -> bool:
        return bool(self.disk is not None and self.capability.eligible and not self.circuit_open)

    def _unavailable_reason(self) -> str:
        if self.circuit_open:
            return "circuit_open"
        if not self.cache_budget.get("known"):
            return "budget_unknown"
        return self.capability.reason or "disabled"

    def _miss(self, reason: str) -> None:
        self.misses += 1
        self.last_miss_reason = reason

    def _admit_restore(self, tokens: int) -> None:
        per_token = int(self.cache_budget.get("perTokenBytes", 0))
        if not self.cache_budget.get("known") or per_token <= 0:
            raise RestoreBudgetRejected("unknown_budget")
        # Live result + concatenate scratch + one loaded block.
        required = per_token * (tokens * 2 + BLOCK_SIZE)
        self._admit_bytes(required)

    @staticmethod
    def _admit_bytes(required: int) -> None:
        if required <= 0:
            raise RestoreBudgetRejected("unknown_allocation")
        try:
            absolute = int(os.environ.get("MLXBAR_MLX_MEMORY_LIMIT_BYTES", "0"))
        except (TypeError, ValueError):
            absolute = 0
        active: int | None = None
        try:
            import mlx.core as mx
            active_fn = getattr(mx, "get_active_memory", None)
            if callable(active_fn):
                measured = int(active_fn())
                if measured >= 0:
                    active = measured
        except Exception:
            active = None
        if absolute <= 0:
            try:
                physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                ratio = float(os.environ.get("MLXBAR_MEMORY_LIMIT_RATIO", "0"))
                absolute = int(physical * ratio) if ratio > 0 else 0
            except (AttributeError, OSError, TypeError, ValueError):
                absolute = 0
        if active is None:
            raise RestoreBudgetRejected("active_memory_unknown")
        if absolute <= 0 or required > max(0, absolute - active):
            raise RestoreBudgetRejected("insufficient_headroom")

    def _restore(self, model, descriptors: list[dict]):
        from mlx_lm.models.cache import make_prompt_cache
        import mlx.core as mx
        combined: list[tuple[object, object]] | None = None
        expected_layout = None
        restored = 0
        for descriptor in descriptors:
            block_hash = descriptor["hash"]
            try:
                expected_block_bytes = int(self.cache_budget.get("perTokenBytes", 0)) * BLOCK_SIZE
                # Safetensors metadata is tiny relative to a KV block. Refuse
                # unexpectedly large files before mlx maps/parses their tensor
                # table, limiting damage from local corruption or tampering.
                max_file_bytes = expected_block_bytes + max(1 << 20, expected_block_bytes // 100)
                arrays, metadata = self.disk.load(block_hash, max_file_bytes=max_file_bytes)
                layout = self._validate_metadata(metadata, descriptor)
                if expected_layout is None:
                    expected_layout = layout
                elif layout != expected_layout:
                    raise LayoutMismatch("layout_changed_inside_chain")
                layer_values = []
                for index in range(self.capability.layers):
                    keys = arrays.get(f"k_{index:04d}")
                    values = arrays.get(f"v_{index:04d}")
                    self._validate_array(keys, layout[index]["keyShape"], layout[index]["keyDtype"])
                    self._validate_array(values, layout[index]["valueShape"], layout[index]["valueDtype"])
                    layer_values.append((keys, values))
                expected_names = {
                    name for index in range(self.capability.layers)
                    for name in (f"k_{index:04d}", f"v_{index:04d}")
                }
                if set(arrays) != expected_names:
                    raise CorruptBlock("array_set_mismatch")
                block_bytes = sum(int(value.nbytes) for pair in layer_values for value in pair)
                if block_bytes != int(self.cache_budget.get("perTokenBytes", 0)) * BLOCK_SIZE:
                    raise LayoutMismatch("restored_block_disagrees_with_model_budget")
                mx.eval(*(value for pair in layer_values for value in pair))
            except CorruptBlock as exc:
                self.corrupt_blocks += 1
                self.disk.quarantine_block(block_hash, str(exc))
                break
            if combined is None:
                combined = layer_values
            else:
                combined = [(mx.concatenate([old_k, new_k], axis=2),
                             mx.concatenate([old_v, new_v], axis=2))
                            for (old_k, old_v), (new_k, new_v) in zip(combined, layer_values)]
                mx.eval(*(value for pair in combined for value in pair))
            restored += BLOCK_SIZE
        if combined is None or restored < MIN_REUSABLE_TOKENS:
            raise CorruptBlock("no_contiguous_blocks")
        cache = make_prompt_cache(model)
        capability = probe_plain_kv(cache)
        if not capability.eligible or len(cache) != len(combined):
            raise LayoutMismatch("fresh_cache_layout_mismatch")
        for layer, (keys, values) in zip(cache, combined):
            layer.state = ((keys, values, restored) if capability.state_arity == 3
                           else (keys, values))
        for layer in cache:
            if int(getattr(layer, "offset", -1)) != restored:
                raise LayoutMismatch("restored_offset_mismatch")
        return cache, restored

    def _validate_metadata(self, metadata: dict, descriptor: dict) -> list[dict]:
        required = {
            "format": str(FORMAT_VERSION), "namespace": self.namespace,
            "hash": descriptor["hash"], "parent": descriptor["parent"],
            "tokenDigest": descriptor["tokens"], "start": str(descriptor["start"]),
            "tokens": str(BLOCK_SIZE),
            "layoutFingerprint": self.capability.layout_fingerprint or "",
        }
        for key, value in required.items():
            if metadata.get(key) != value:
                raise CorruptBlock(f"metadata_{key}_mismatch")
        try:
            layout = json.loads(metadata["layout"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptBlock("metadata_layout_invalid") from exc
        if not isinstance(layout, list) or len(layout) != self.capability.layers:
            raise CorruptBlock("metadata_layer_count_mismatch")
        for item in layout:
            if not isinstance(item, dict) or set(item) != {
                    "keyShape", "valueShape", "keyDtype", "valueDtype"}:
                raise CorruptBlock("metadata_layout_shape_invalid")
            for shape_key in ("keyShape", "valueShape"):
                shape = item[shape_key]
                if (not isinstance(shape, list) or len(shape) != 3
                        or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                               for value in shape)):
                    raise CorruptBlock("metadata_layout_shape_invalid")
            if not all(isinstance(item[key], str) and item[key]
                       for key in ("keyDtype", "valueDtype")):
                raise CorruptBlock("metadata_layout_dtype_invalid")
        return layout

    @staticmethod
    def _validate_array(value, non_sequence_shape: list[int], dtype: str) -> None:
        if value is None or len(value.shape) != 4:
            raise CorruptBlock("array_rank_mismatch")
        actual = [int(value.shape[0]), int(value.shape[1]), int(value.shape[3])]
        if actual != [int(item) for item in non_sequence_shape] or int(value.shape[2]) != BLOCK_SIZE:
            raise CorruptBlock("array_shape_mismatch")
        if str(value.dtype) != str(dtype):
            raise CorruptBlock("array_dtype_mismatch")

    def _restore_failed(self, reason: str, detail: str, immediate: bool = False) -> None:
        LOGGER.warning("Paged KV restore skipped (%s): %s", reason, detail)
        self._miss(reason)
        self.consecutive_restore_failures += 1
        if immediate or self.consecutive_restore_failures >= 3:
            self.circuit_open = True
