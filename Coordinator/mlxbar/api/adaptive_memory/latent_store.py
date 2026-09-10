"""Content-addressed note cache for the coordinator LATENT tier.

Keyed only by content -- ``SHA256(canonical message + role + model fingerprint
+ policy version)`` -- not by conversation id. When ZCode / Claude Code /
OpenCode re-send the same history, an identical LATENT run hashes to the same
key and the summarizer sub-call is skipped.

Best-effort throughout: a read that hits a truncated / unparsable file, or any
OS error, is treated as a miss; a write that fails is swallowed. Losing the
cache costs a summarizer call, never a request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_MAX_FILES = 2000
_MAX_AGE_SECONDS = 30 * 24 * 3600


def note_key(canonical: str, role: str, model_fingerprint: str, policy_version: str) -> str:
    digest = hashlib.sha256()
    for part in (canonical, "\x00", role, "\x00", model_fingerprint, "\x00", policy_version):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


class NoteCache:
    def __init__(self, root: Path | None):
        # root is None -> in-memory only (persistentLatentCache off / no home).
        self.dir = (Path(root) / "adaptive-memory" / "notes") if root else None
        self._memory: dict[str, str] = {}
        if self.dir is not None:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem dependent
                LOGGER.warning("adaptive-memory note cache disabled: %s", exc)
                self.dir = None

    def get(self, key: str) -> str | None:
        if key in self._memory:
            return self._memory[key]
        if self.dir is None:
            return None
        path = self.dir / f"{key}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            note = payload["note"]
            if not isinstance(note, str) or not note:
                return None
            self._memory[key] = note
            return note
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, key: str, note: str) -> None:
        if not note:
            return
        self._memory[key] = note
        if self.dir is None:
            return
        path = self.dir / f"{key}.json"
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"note": note, "at": time.time()}, ensure_ascii=False),
                           encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(path)
            self._prune()
        except OSError as exc:  # pragma: no cover - filesystem dependent
            LOGGER.debug("adaptive-memory note cache write failed: %s", exc)

    def _prune(self) -> None:
        try:
            entries = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        now = time.time()
        stale = [p for p in entries if now - p.stat().st_mtime > _MAX_AGE_SECONDS]
        overflow = entries[:-_MAX_FILES] if len(entries) > _MAX_FILES else []
        for path in {*stale, *overflow}:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
