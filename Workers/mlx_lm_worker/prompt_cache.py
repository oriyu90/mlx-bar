"""Two-tier prompt cache for the mlx-lm worker.

mlx-vlm has shipped prompt reuse since v1.3.7 (RAM) and v1.4.0 (disk), but the
mlx-lm path had neither, so a text-only model re-prefilled the entire ZCode
system prompt, tool schema and history on every turn.

The warm tier is mlx-lm's own ``LRUPromptCache``: a trie of previously seen
token sequences that returns the nearest reusable cache plus the tokens still
left to process. The cold tier persists one guarded prefix snapshot per prompt
shape so the first request after a worker restart does not pay full prefill
either.

Every disk operation is best-effort. A cache that cannot be read or written is
dropped, never raised: losing reuse costs time, but failing the generation
costs the answer.
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

LOGGER = logging.getLogger(__name__)

INDEX_NAME = "index.json"
SNAPSHOT_SUFFIX = ".safetensors"
# Keep the tail of the prompt out of the persisted prefix so that changing only
# the newest user message still hits the large, stable system/tools prefix.
DEFAULT_GUARD_TOKENS = 256
# Below this a cache lookup costs more than the prefill it saves.
MIN_REUSABLE_TOKENS = 64


class PromptCacheStore:
    """Warm in-memory reuse plus a persistent prefix snapshot."""

    def __init__(self, model_path: str, runtime_version: str, *, root: str | None = None,
                 disk_enabled: bool = True, max_bytes: int = 10 << 30,
                 keep_generations: int = 2, guard_tokens: int = DEFAULT_GUARD_TOKENS,
                 memory_max_bytes: int | None = None):
        self.model_path = model_path
        self.guard_tokens = max(0, guard_tokens)
        self.max_bytes = max(0, max_bytes)
        self.namespace: str | None = None
        self.root: Path | None = None
        self.disabled_reason: str | None = None
        self.memory: object | None = None
        self.disk_hits = 0
        self.memory_hits = 0
        # A single 8k-token snapshot of a 32B model is around a gigabyte, so an
        # unbounded warm tier would quietly undo the memory limits this release
        # adds. Cap it against physical RAM unless told otherwise.
        self.memory_max_bytes = memory_max_bytes or self._default_memory_budget()
        self._reset_memory(self.memory_max_bytes)
        if not disk_enabled:
            self.disabled_reason = "disabled_by_setting"
            return
        if not root:
            self.disabled_reason = "cache_root_missing"
            return
        try:
            self.namespace = f"mlxbar-lm-v1-{self._fingerprint(Path(model_path), runtime_version)}"
            self.root = Path(root)
            (self.root / self.namespace).mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            os.chmod(self.root / self.namespace, 0o700)
            self._sweep_stale_namespaces(max(1, keep_generations))
        except Exception as exc:
            self.root = None
            self.namespace = None
            self.disabled_reason = f"initialization_failed:{type(exc).__name__}"
            LOGGER.warning("Disk prompt cache disabled: %s", exc)

    # ---------------------------------------------------------------- memory

    def _reset_memory(self, memory_max_bytes: int | None = None) -> None:
        try:
            from mlx_lm.models.cache import LRUPromptCache
        except (ImportError, AttributeError):
            # Older runtime slots stay usable, just without reuse.
            self.memory = None
            return
        limit = memory_max_bytes if memory_max_bytes and memory_max_bytes > 0 else self.memory_max_bytes
        self.memory = LRUPromptCache(max_size=4, **({"max_bytes": limit} if limit else {}))

    @staticmethod
    def _default_memory_budget() -> int:
        try:
            ratio = float(os.environ.get("MLXBAR_PROMPT_CACHE_MEMORY_RATIO", "0.10"))
        except (TypeError, ValueError):
            ratio = 0.10
        if ratio <= 0:
            return 0
        try:
            physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            return 0
        return int(physical * ratio)

    def clear_memory(self) -> None:
        self._reset_memory()

    def trim_memory_to(self, n_bytes: int) -> None:
        """Give memory back without losing the disk tier."""
        if self.memory is None:
            return
        with contextlib.suppress(Exception):
            self.memory.trim_to(n_bytes=max(0, n_bytes))

    # ------------------------------------------------------------ public API

    @property
    def model_key(self) -> str:
        """Hashable identity for the loaded model.

        `LRUPromptCache` keys its trie with a plain dict lookup, so the model
        object itself cannot be used -- an `nn.Module` is unhashable, and
        passing one makes every lookup raise and silently disable the tier.
        mlx-lm's own server passes a key string for the same reason.
        """
        return f"{self.namespace or 'mlxbar-lm'}:{self.model_path}"

    def fetch(self, model, tokens: list[int]):
        """Return ``(cache, remaining_tokens, tier)`` for this prompt.

        ``cache`` is None when nothing is reusable, in which case the caller
        prefills from scratch. ``remaining_tokens`` is always what still has to
        be processed, so the caller never needs to know which tier answered.
        """
        if not tokens:
            return None, tokens, "cold"
        cache, remaining = self._fetch_memory(self.model_key, tokens)
        if cache is not None and len(remaining) < len(tokens):
            self.memory_hits += 1
            return cache, remaining, "memory"
        snapshot = self._fetch_disk(model, tokens)
        if snapshot is not None:
            self.disk_hits += 1
            return snapshot[0], tokens[snapshot[1]:], "disk"
        return None, tokens, "cold"

    def store(self, model, tokens: list[int], cache, prompt_length: int | None = None) -> None:
        """Record a completed generation's cache in both tiers.

        ``prompt_length`` bounds what may be persisted: only the prompt is a
        prefix a later request can share. Persisting into the generated reply
        would write a snapshot that no changed follow-up can ever match, at
        roughly a gigabyte apiece.
        """
        if cache is None or not tokens:
            return
        if self.memory is not None:
            try:
                self.memory.insert_cache(self.model_key, list(tokens), cache)
            except Exception as exc:
                LOGGER.warning("Could not retain prompt cache in memory: %s", exc)
                self._reset_memory()
        self._store_disk(model, tokens, cache, prompt_length or len(tokens))

    def stats(self) -> dict:
        result = {
            "enabled": self.memory is not None or self.root is not None,
            "engine": "mlx-lm",
            "memory": self.memory is not None,
            "disk": self.root is not None,
            "namespace": self.namespace,
            "disabledReason": self.disabled_reason,
            "memoryHits": self.memory_hits,
            "diskHits": self.disk_hits,
            "generations": self._namespace_count(),
            "diskBytes": self._namespace_bytes(),
        }
        if self.memory is not None:
            with contextlib.suppress(Exception):
                result["memoryEntries"] = len(self.memory)
                result["memoryBytes"] = int(self.memory.nbytes)
        return result

    def clear_disk(self) -> None:
        if self.root is None or not self.namespace:
            return
        directory = self.root / self.namespace
        with contextlib.suppress(OSError):
            shutil.rmtree(directory)
        with contextlib.suppress(OSError):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        self.disk_hits = 0

    # ------------------------------------------------------------ memory tier

    def _fetch_memory(self, model_key: str, tokens: list[int]):
        if self.memory is None:
            return None, tokens
        try:
            cache, remaining = self.memory.fetch_nearest_cache(model_key, list(tokens))
        except Exception as exc:
            LOGGER.warning("Prompt cache lookup failed; falling back to cold prefill: %s", exc)
            self._reset_memory()
            return None, tokens
        if cache is None:
            return None, tokens
        # A reuse that saves only a handful of tokens is not worth the copy.
        if len(tokens) - len(remaining) < MIN_REUSABLE_TOKENS:
            return None, tokens
        # mlx-lm requires at least one token to run the forward pass on.
        if not remaining:
            return None, tokens
        return cache, list(remaining)

    # -------------------------------------------------------------- disk tier

    @property
    def _directory(self) -> Path | None:
        if self.root is None or not self.namespace:
            return None
        return self.root / self.namespace

    def _read_index(self) -> list[dict]:
        directory = self._directory
        if directory is None:
            return []
        try:
            data = json.loads((directory / INDEX_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write_index(self, entries: list[dict]) -> None:
        directory = self._directory
        if directory is None:
            return
        temporary = directory / (INDEX_NAME + ".tmp")
        with contextlib.suppress(OSError, TypeError, ValueError):
            temporary.write_text(json.dumps(entries), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(directory / INDEX_NAME)

    @staticmethod
    def _token_digest(tokens: list[int]) -> str:
        digest = hashlib.sha256()
        digest.update(b"mlxbar-lm-prefix-v1\0")
        for token in tokens:
            digest.update(int(token).to_bytes(4, "little", signed=False))
        return digest.hexdigest()

    def _fetch_disk(self, model, tokens: list[int]):
        directory = self._directory
        if directory is None:
            return None
        try:
            from mlx_lm.models.cache import load_prompt_cache
        except (ImportError, AttributeError):
            return None
        entries = self._read_index()
        # Longest usable prefix first.
        for entry in sorted(entries, key=lambda item: int(item.get("tokens", 0)), reverse=True):
            length = int(entry.get("tokens", 0))
            # Leave at least one token for the forward pass.
            if length < MIN_REUSABLE_TOKENS or length >= len(tokens):
                continue
            if entry.get("digest") != self._token_digest(tokens[:length]):
                continue
            path = directory / str(entry.get("file", ""))
            if not path.is_file():
                continue
            try:
                cache = load_prompt_cache(str(path))
            except Exception as exc:
                LOGGER.warning("Discarding unreadable prompt-cache snapshot %s: %s", path.name, exc)
                with contextlib.suppress(OSError):
                    path.unlink()
                self._write_index([item for item in entries if item is not entry])
                continue
            with contextlib.suppress(OSError):
                os.utime(path)
            return cache, length
        return None

    def _store_disk(self, model, tokens: list[int], cache, prompt_length: int) -> None:
        directory = self._directory
        if directory is None:
            return
        try:
            from mlx_lm.models.cache import (can_trim_prompt_cache, save_prompt_cache,
                                             trim_prompt_cache)
        except (ImportError, AttributeError):
            return
        prefix_length = min(len(tokens), prompt_length) - self.guard_tokens
        if prefix_length < MIN_REUSABLE_TOKENS:
            return
        entries = self._read_index()
        digest = self._token_digest(tokens[:prefix_length])
        if any(item.get("digest") == digest for item in entries):
            return
        if not can_trim_prompt_cache(cache):
            return
        try:
            import copy

            snapshot = copy.deepcopy(cache)
            surplus = len(tokens) - prefix_length
            if surplus > 0:
                trim_prompt_cache(snapshot, surplus)
            name = f"{digest[:16]}{SNAPSHOT_SUFFIX}"
            path = directory / name
            save_prompt_cache(str(path), snapshot,
                              metadata={"tokens": str(prefix_length), "model": self.model_path})
            os.chmod(path, 0o600)
        except Exception as exc:
            LOGGER.warning("Could not persist prompt cache: %s", exc)
            return
        entries.append({"file": name, "tokens": prefix_length, "digest": digest,
                        "savedAt": time.time()})
        self._write_index(entries)
        self._enforce_disk_budget()

    def _enforce_disk_budget(self) -> None:
        directory = self._directory
        if directory is None or self.max_bytes <= 0:
            return
        entries = self._read_index()
        snapshots = []
        for entry in entries:
            path = directory / str(entry.get("file", ""))
            try:
                snapshots.append((path.stat().st_mtime, path.stat().st_size, entry, path))
            except OSError:
                continue
        total = sum(size for _, size, _, _ in snapshots)
        if total <= self.max_bytes:
            return
        # Evict least recently used until the budget holds.
        snapshots.sort(key=lambda item: item[0])
        surviving = list(entries)
        for _, size, entry, path in snapshots:
            if total <= self.max_bytes:
                break
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size
            surviving = [item for item in surviving if item is not entry]
        self._write_index(surviving)

    # ------------------------------------------------------------- namespaces

    @staticmethod
    def _fingerprint(path: Path, runtime_version: str) -> str:
        """Identify cache compatibility without reading multi-GB weights."""
        digest = hashlib.sha256()
        digest.update(b"mlxbar-lm-cache-v1\0")
        digest.update(str(path.resolve()).encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(runtime_version.encode("ascii", errors="replace"))
        metadata_names = {"config.json", "tokenizer.json", "tokenizer_config.json",
                          "chat_template.jinja", "model.safetensors.index.json"}
        try:
            entries = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError:
            entries = []
        for entry in entries:
            if entry.name in metadata_names:
                with contextlib.suppress(OSError):
                    digest.update(entry.name.encode("utf-8"))
                    digest.update(entry.read_bytes())
            elif entry.suffix == ".safetensors":
                with contextlib.suppress(OSError):
                    stat = entry.stat()
                    digest.update(entry.name.encode("utf-8"))
                    digest.update(str(stat.st_size).encode("ascii"))
                    digest.update(str(stat.st_mtime_ns).encode("ascii"))
        return digest.hexdigest()

    def _sweep_stale_namespaces(self, keep: int) -> list[str]:
        """Drop cache generations this model/runtime combination cannot read.

        The namespace fingerprints the model files and the runtime version, so
        each model switch or runtime update starts a new one and the byte
        budget only ever bounds a single generation.
        """
        if self.root is None or not self.namespace:
            return []
        try:
            entries = [item for item in self.root.iterdir()
                       if item.is_dir() and item.name != self.namespace]
        except OSError:
            return []

        def modified(item: Path) -> float:
            try:
                return item.stat().st_mtime
            except OSError:
                return 0.0

        entries.sort(key=modified, reverse=True)
        removed = []
        for stale in entries[max(0, keep - 1):]:
            try:
                shutil.rmtree(stale)
                removed.append(stale.name)
            except OSError as exc:
                LOGGER.warning("Stale cache sweep failed for %s: %s", stale.name, exc)
        return removed

    def _namespace_count(self) -> int:
        if self.root is None:
            return 0
        try:
            return sum(1 for item in self.root.iterdir() if item.is_dir())
        except OSError:
            return 0

    def _namespace_bytes(self) -> int:
        total = 0
        if self.root is None:
            return total
        with contextlib.suppress(OSError):
            for item in self.root.rglob("*"):
                with contextlib.suppress(OSError):
                    if item.is_file():
                        total += item.stat().st_size
        return total
