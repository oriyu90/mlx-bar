"""Prefix reuse for the mlx-vlm worker that does not depend on ``trim``.

mlx-vlm reuses a retained cache by rolling it back to the shared prefix, which
only works for architectures whose every cache component can drop trailing
tokens. Hybrid models -- Qwen3.5/3.8 and anything else mixing recurrent layers
with full attention -- cannot: a recurrent state has no "last N tokens" to
remove. Before v1.6.0 that produced a full cold prefill on every branching
turn, and a cancelled generation threw the warm cache away entirely.

Two pieces replace that:

``GuardedPromptCacheState``
    A subclass of the runtime's own ``PromptCacheState``. It answers
    ``find_prefix_length`` itself, so a rollback the architecture cannot perform
    is refused *before* the runtime reaches ``trim``, and a snapshot that
    matches the new prompt can be swapped in instead.

``CheckpointStore``
    Captures the cache at the end of a prompt -- the boundary a later request
    can actually share -- and keeps it in memory and on disk. Restoring a
    capture is what gives a hybrid model the rollback its cache type denies.

Both degrade to a cold prefill rather than raise. Reuse is an optimisation; the
answer is not.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from common import cache_state

LOGGER = logging.getLogger(__name__)

INDEX_NAME = "index.json"
# Below this a lookup and a restore cost more than the prefill they save.
MIN_REUSABLE_TOKENS = 256


def token_digest(tokens: list[int]) -> str:
    digest = hashlib.sha256()
    digest.update(b"mlxbar-vlm-prefix-v1\0")
    for token in tokens:
        digest.update(int(token).to_bytes(4, "little", signed=False))
    return digest.hexdigest()


def is_prefix(candidate: list[int], full: list[int]) -> bool:
    return len(candidate) < len(full) and full[:len(candidate)] == candidate


class CheckpointStore:
    """Prompt-boundary snapshots, in memory and on disk.

    Only the prompt is captured, never the prompt plus the model's own reply:
    a snapshot taken past the prompt can never match a follow-up whose reply
    differs, and at roughly a gigabyte apiece those writes are pure loss. This
    is the same rule the mlx-lm store learned in v1.5.0.
    """

    def __init__(self, *, root: Path | None, namespace: str | None, budget: dict,
                 max_bytes: int, keep_generations: int, write_budget_bytes: int,
                 disk_enabled: bool = True):
        self.budget = budget or {}
        self.max_bytes = max(0, int(max_bytes))
        self.write_budget_bytes = max(0, int(write_budget_bytes))
        self.bytes_written = 0
        self.disabled_reason: str | None = None
        self.root = root if disk_enabled else None
        self.namespace = namespace
        self.memory_ids: list[int] | None = None
        self.memory_payload: list | None = None
        self.memory_hits = 0
        self.disk_hits = 0
        self.captures = 0
        self.capture_failures = 0
        if not disk_enabled:
            self.disabled_reason = "disabled_by_setting"
        elif root is None or not namespace:
            self.disabled_reason = "cache_root_missing"
        else:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                os.chmod(self.directory, 0o700)
                self._sweep_stale(max(1, int(keep_generations)))
            except OSError as exc:
                self.root = None
                self.disabled_reason = f"initialization_failed:{type(exc).__name__}"
                LOGGER.warning("Disk checkpoint store disabled: %s", exc)

    # ------------------------------------------------------------ locations

    @property
    def directory(self) -> Path:
        return (self.root or Path(".")) / (self.namespace or "default")

    def _sweep_stale(self, keep: int) -> None:
        """Drop generations this model or runtime can no longer read.

        The namespace fingerprints the weights *and* the runtime version, so a
        runtime update leaves its predecessor's snapshots unreadable. Keeping a
        couple lets a rollback to the previous slot still hit its own cache.
        """
        if self.root is None:
            return
        try:
            generations = sorted((item for item in self.root.iterdir() if item.is_dir()),
                                 key=lambda item: item.stat().st_mtime, reverse=True)
        except OSError:
            return
        for stale in generations[keep:]:
            if stale.name == self.namespace:
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(stale)

    # -------------------------------------------------------------- budget

    def snapshot_bytes(self, tokens: int) -> int:
        return cache_state.snapshot_bytes(self.budget, tokens)

    def affordable_tokens(self) -> int:
        return cache_state.affordable_tokens(self.budget, self.max_bytes)

    def budget_allows(self, tokens: int) -> bool:
        """Whether one snapshot of this length can be stored at all.

        A limit that cannot hold a single snapshot is worse than no disk tier:
        every turn writes a gigabyte that is immediately evicted, so the SSD
        wear is real and the reuse is zero.
        """
        if not self.budget.get("known"):
            return True  # unknown size: keep the previous behaviour, do not guess
        required = self.snapshot_bytes(tokens)
        return required > 0 and required <= self.max_bytes

    # ------------------------------------------------------------- capture

    def remember(self, ids: list[int], cache: Any) -> bool:
        """Capture the cache at the end of ``ids`` as the reusable boundary."""
        if not ids or cache is None:
            return False
        # Release the superseded snapshot before building its replacement.
        # Holding both while the copy runs would put three full caches in
        # unified memory at once, and on a long conversation the third one is
        # measured in gigabytes. A snapshot about to be replaced is the
        # cheapest thing in the process to lose.
        self.memory_ids = None
        self.memory_payload = None
        payload = cache_state.capture(cache)
        if payload is None:
            self.capture_failures += 1
            return False
        try:
            cache_state.evaluate(payload)
        except Exception as exc:
            self.capture_failures += 1
            LOGGER.warning("Prompt cache capture could not be materialised: %s", exc)
            return False
        self.memory_ids = list(ids)
        self.memory_payload = payload
        self.captures += 1
        return True

    def forget(self) -> None:
        self.memory_ids = None
        self.memory_payload = None

    # ------------------------------------------------------------- restore

    def restore_best(self, new_ids: list[int], make_cache: Callable[[], Any]) -> tuple | None:
        """Longest snapshot that is a prefix of ``new_ids``.

        Returns ``(cache, ids, tier)`` or None. The memory tier is tried first
        because restoring it costs one copy and no I/O.
        """
        if not new_ids:
            return None
        best = self._restore_memory(new_ids, make_cache)
        if best is not None:
            return best
        return self._restore_disk(new_ids, make_cache)

    def _restore_memory(self, new_ids: list[int], make_cache) -> tuple | None:
        ids, payload = self.memory_ids, self.memory_payload
        if not ids or not payload or len(ids) < MIN_REUSABLE_TOKENS:
            return None
        if not is_prefix(ids, new_ids):
            return None
        cache = self._materialise(payload, make_cache)
        if cache is None:
            return None
        self.memory_hits += 1
        return cache, list(ids), "memory"

    def _restore_disk(self, new_ids: list[int], make_cache) -> tuple | None:
        if self.root is None:
            return None
        entries = self._read_index()
        for entry in sorted(entries, key=lambda item: int(item.get("tokens", 0)), reverse=True):
            length = int(entry.get("tokens", 0))
            if length < MIN_REUSABLE_TOKENS or length >= len(new_ids):
                continue
            if entry.get("digest") != token_digest(new_ids[:length]):
                continue
            payload = self._read_snapshot(str(entry.get("file", "")))
            if payload is None:
                self._drop_entry(entry)
                continue
            cache = self._materialise(payload, make_cache)
            if cache is None:
                continue
            with contextlib.suppress(OSError):
                os.utime(self.directory / str(entry.get("file")))
            self.disk_hits += 1
            return cache, list(new_ids[:length]), "disk"
        return None

    def _materialise(self, payload: list, make_cache) -> Any | None:
        try:
            cache = make_cache()
        except Exception as exc:
            LOGGER.warning("Could not build a cache to restore into: %s", exc)
            return None
        if not cache_state.restore(cache, payload):
            return None
        return cache

    # ---------------------------------------------------------------- disk

    def _read_index(self) -> list[dict]:
        if self.root is None:
            return []
        try:
            data = json.loads((self.directory / INDEX_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write_index(self, entries: list[dict]) -> None:
        if self.root is None:
            return
        temporary = self.directory / (INDEX_NAME + ".tmp")
        with contextlib.suppress(OSError, TypeError, ValueError):
            temporary.write_text(json.dumps(entries), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.directory / INDEX_NAME)

    def _drop_entry(self, entry: dict) -> None:
        entries = [item for item in self._read_index() if item.get("digest") != entry.get("digest")]
        with contextlib.suppress(OSError):
            (self.directory / str(entry.get("file", ""))).unlink()
        with contextlib.suppress(OSError):
            (self.directory / f"{entry.get('digest')}.json").unlink()
        self._write_index(entries)

    def _read_snapshot(self, name: str) -> list | None:
        if self.root is None or not name:
            return None
        path = self.directory / name
        manifest_path = path.with_suffix(".json")
        if not path.is_file() or not manifest_path.is_file():
            return None
        try:
            import mlx.core as mx
            arrays = mx.load(str(path))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return cache_state.unflatten(arrays, manifest)
        except Exception as exc:
            LOGGER.warning("Discarding unreadable prompt-cache snapshot %s: %s", name, exc)
            return None

    def persist(self, ids: list[int], payload: list | None = None) -> str | None:
        """Write the remembered boundary to disk. Returns a cold reason on refusal."""
        if self.root is None:
            return self.disabled_reason
        payload = payload if payload is not None else self.memory_payload
        ids = list(ids or self.memory_ids or [])
        if not payload or len(ids) < MIN_REUSABLE_TOKENS:
            return None
        if not self.budget_allows(len(ids)):
            self.disabled_reason = cache_state.COLD_BUDGET_INSUFFICIENT
            return cache_state.COLD_BUDGET_INSUFFICIENT
        size = cache_state.payload_bytes(payload)
        if self.write_budget_bytes and self.bytes_written + size > self.write_budget_bytes:
            self.disabled_reason = cache_state.COLD_WRITE_BUDGET_REACHED
            return cache_state.COLD_WRITE_BUDGET_REACHED
        digest = token_digest(ids)
        entries = [item for item in self._read_index() if item.get("digest") != digest]
        name = f"{digest}.safetensors"
        try:
            import mlx.core as mx
            arrays, manifest = cache_state.flatten(payload)
            temporary = self.directory / (name + ".tmp")
            mx.save_safetensors(str(temporary), arrays)
            os.chmod(temporary, 0o600)
            temporary.replace(self.directory / name)
            manifest_path = self.directory / f"{digest}.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            os.chmod(manifest_path, 0o600)
        except Exception as exc:
            LOGGER.warning("Could not persist prompt cache snapshot: %s", exc)
            with contextlib.suppress(OSError):
                (self.directory / (name + ".tmp")).unlink()
            return None
        self.bytes_written += size
        entries.append({"tokens": len(ids), "digest": digest, "file": name,
                        "bytes": size, "savedAt": time.time()})
        self._evict(entries)
        return None

    def _evict(self, entries: list[dict]) -> None:
        """Keep the newest snapshots that fit under the byte limit."""
        entries.sort(key=lambda item: float(item.get("savedAt", 0)), reverse=True)
        kept: list[dict] = []
        total = 0
        for entry in entries:
            size = int(entry.get("bytes", 0))
            if self.max_bytes and total + size > self.max_bytes and kept:
                with contextlib.suppress(OSError):
                    (self.directory / str(entry.get("file", ""))).unlink()
                with contextlib.suppress(OSError):
                    (self.directory / f"{entry.get('digest')}.json").unlink()
                continue
            total += size
            kept.append(entry)
        self._write_index(kept)

    def clear_disk(self) -> None:
        if self.root is None:
            return
        with contextlib.suppress(OSError):
            shutil.rmtree(self.directory)
        with contextlib.suppress(OSError):
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
        self.disk_hits = 0
        self.bytes_written = 0

    def disk_bytes(self) -> int:
        if self.root is None:
            return 0
        total = 0
        with contextlib.suppress(OSError):
            for item in self.directory.rglob("*"):
                with contextlib.suppress(OSError):
                    if item.is_file():
                        total += item.stat().st_size
        return total

    def stats(self) -> dict:
        return {"memoryHits": self.memory_hits, "diskHits": self.disk_hits,
                "captures": self.captures, "captureFailures": self.capture_failures,
                "bytesWritten": self.bytes_written,
                "checkpointTokens": len(self.memory_ids or []),
                "affordableTokens": self.affordable_tokens(),
                "disabledReason": self.disabled_reason}


def build_guarded_state(base_class, controller) -> Any:
    """Subclass the runtime's PromptCacheState with MLXBar's rollback policy.

    The base class comes from the installed runtime, so the subclass is built at
    call time rather than at import. Only two documented interactions are used:
    the runtime asks for a shared prefix length, then reads the cache. Nothing
    reaches into the runtime's internals.
    """

    class GuardedPromptCacheState(base_class):
        def __init__(self):
            self._cache_reads = 0
            self._last_probe = None
            super().__init__()

        # `cache` is a property so the state can tell whether the runtime read
        # it before or after asking for the prefix length. A snapshot may only
        # be swapped in when the read comes after: swapping under a caller that
        # already holds the previous cache would pair one cache with another
        # cache's prefix length, which is silent corruption rather than a slow
        # request.
        @property
        def cache(self):
            self._cache_reads += 1
            return self._cache

        @cache.setter
        def cache(self, value):
            self._cache = value

        def begin_request(self) -> None:
            self._cache_reads = 0
            self._last_probe = None

        @property
        def last_probe(self) -> dict | None:
            return self._last_probe

        def find_prefix_length(self, new_ids: list) -> int:
            held = list(self.token_ids or [])
            natural = super().find_prefix_length(new_ids)
            probe = {"promptTokens": len(new_ids), "heldTokens": len(held),
                     "sharedPrefix": int(natural), "action": "reuse", "restoredFrom": None}
            controller.observe_prompt(list(new_ids))
            if natural <= 0:
                probe["action"] = "cold"
                probe["reason"] = cache_state.COLD_NO_PREFIX if held else cache_state.COLD_FIRST_REQUEST
                self._last_probe = probe
                return 0
            if natural >= len(held):
                # Pure continuation: the retained cache is already the prefix,
                # so no rollback is involved and every architecture can reuse it.
                self._last_probe = probe
                return natural
            # A rollback is required. Ask the cache itself whether it can do one.
            if cache_state.can_trim(self._cache):
                probe["action"] = "trim"
                self._last_probe = probe
                return natural
            restored = None
            if self._cache_reads == 0:
                # Release the retained cache before building the replacement.
                # On this path it is already lost either way -- it cannot be
                # rolled back, and whatever happens next replaces it -- so
                # holding it through the restore only puts a third full cache in
                # unified memory at the moment there is least room for one.
                self._cache = None
                restored = controller.restore_for(list(new_ids))
            if restored is not None:
                cache, ids, tier = restored
                self._cache = cache
                self.token_ids = list(ids)
                probe.update({"action": "restore", "restoredFrom": tier,
                              "sharedPrefix": len(ids)})
                self._last_probe = probe
                return len(ids)
            probe.update({"action": "cold", "reason": cache_state.COLD_REUSE_UNSUPPORTED})
            self._last_probe = probe
            # Refusing here is what keeps the runtime away from `trim()` on a
            # component that does not define it. Returning 0 makes mlx-vlm skip
            # its reuse branch entirely and fall through to its own disk lookup.
            return 0

    return GuardedPromptCacheState()
