"""Content-addressed Latent Cache for soft tokens.

Key = ``SHA256(band tokens + reader fingerprint + writer fingerprint + policy
version)`` -- conversation-id independent, so the same history re-sent by any
client reuses the Writer's output. RAM LRU always; disk (``mx.save``/``mx.load``
of an ``.npy``) when ``persistentLatentCache`` is on and a root is available.

Best-effort: an unreadable / dimension-mismatched entry is a miss, a failed
write is swallowed. A miss only costs one Writer pass.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import OrderedDict
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_RAM_ENTRIES = 64
_DISK_ENTRIES = 512


def latent_key(band_tokens: list[int], reader_fp: str, writer_fp: str, policy_version: str) -> str:
    digest = hashlib.sha256()
    digest.update(reader_fp.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(writer_fp.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(policy_version.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(bytes(memoryview(_as_u32(band_tokens))))
    return digest.hexdigest()


def _as_u32(values: list[int]):
    import array
    return array.array("I", (v & 0xFFFFFFFF for v in values))


class LatentCache:
    def __init__(self, root: str | None, hidden: int):
        self._ram: "OrderedDict[str, object]" = OrderedDict()
        self._hidden = hidden
        self.dir: Path | None = None
        if root:
            try:
                self.dir = Path(root) / "adaptive-latent"
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover
                LOGGER.warning("latent cache disk tier disabled: %s", exc)
                self.dir = None

    def get(self, key: str):
        if key in self._ram:
            self._ram.move_to_end(key)
            return self._ram[key]
        if self.dir is None:
            return None
        path = self.dir / f"{key}.npy"
        if not path.exists():
            return None
        try:
            import mlx.core as mx
            value = mx.load(str(path))
            if value.ndim != 2 or value.shape[1] != self._hidden:
                return None
            self._remember(key, value)
            return value
        except Exception as exc:  # noqa: BLE001 - any load failure is a miss
            LOGGER.debug("latent cache load miss (%s): %s", key[:12], exc)
            return None

    def put(self, key: str, value) -> None:
        self._remember(key, value)
        if self.dir is None:
            return
        try:
            import mlx.core as mx
            path = self.dir / f"{key}.npy"
            tmp = self.dir / f"{key}.npy.tmp"
            mx.save(str(tmp), value)
            os.replace(tmp, path)
            self._prune_disk()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("latent cache store skipped: %s", exc)

    def _remember(self, key: str, value) -> None:
        self._ram[key] = value
        self._ram.move_to_end(key)
        while len(self._ram) > _RAM_ENTRIES:
            self._ram.popitem(last=False)

    def _prune_disk(self) -> None:
        try:
            entries = sorted(self.dir.glob("*.npy"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in entries[:-_DISK_ENTRIES] if len(entries) > _DISK_ENTRIES else []:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
