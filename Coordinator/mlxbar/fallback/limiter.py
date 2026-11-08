"""Per-model rate windows and failure cooldowns (DESIGN_v5.0.0.md F-4).

Memory-bounded on purpose: the table holds at most ``MAX_ENTRIES`` keys and
evicts the least recently used one, so a long-running coordinator that has seen
many providers/models cannot grow without bound. Nothing is persisted -- a
restart clears the windows, which is the conservative direction (a fresh
process starts with a full budget rather than inheriting a stale one).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_ENTRIES = 256
#: Hard ceiling on the timestamps kept for one window (review M-3).
MAX_STAMPS = 4096


@dataclass
class _Window:
    #: Monotonic timestamps of the requests sent inside the current window.
    stamps: list[float] = field(default_factory=list)
    #: Monotonic time of the last failure, or None.
    failed_at: float | None = None
    #: The message of the last failure, for the status endpoint.
    last_error: str | None = None


class FallbackLimiter:
    """Tracks rate windows and failure cooldowns for external models."""

    def __init__(self, clock=time.monotonic, max_entries: int = MAX_ENTRIES):
        self._clock = clock
        self._max_entries = max(1, int(max_entries))
        self._entries: "OrderedDict[str, _Window]" = OrderedDict()

    # -- bookkeeping ------------------------------------------------------
    def _window(self, key: str, *, touch: bool = True) -> _Window:
        window = self._entries.get(key)
        if window is None:
            window = _Window()
            self._entries[key] = window
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        elif touch:
            self._entries.move_to_end(key)
        return window

    def _prune(self, window: _Window, window_seconds: int, now: float) -> None:
        # L-5: always filter. The old `stamps[0] < cutoff` shortcut skipped the
        # prune whenever the oldest entry was still inside the window, which is
        # exactly the state a cooldown leaves behind.
        if not window.stamps:
            return
        cutoff = now - max(1, int(window_seconds))
        window.stamps = [stamp for stamp in window.stamps if stamp >= cutoff]

    # -- queries ----------------------------------------------------------
    def is_limited(self, key: str, maximum: int, window_seconds: int) -> bool:
        """Whether ``maximum`` requests were already sent in the window."""
        if not maximum or maximum <= 0:
            return False
        window = self._entries.get(key)
        if window is None:
            return False
        self._prune(window, window_seconds, self._clock())
        return len(window.stamps) >= maximum

    def is_cooling(self, key: str, cooldown_seconds: int) -> bool:
        if cooldown_seconds <= 0:
            return False
        window = self._entries.get(key)
        if window is None or window.failed_at is None:
            return False
        return (self._clock() - window.failed_at) < cooldown_seconds

    def usage(self, key: str, window_seconds: int) -> int:
        window = self._entries.get(key)
        if window is None:
            return 0
        self._prune(window, window_seconds, self._clock())
        return len(window.stamps)

    # -- mutations --------------------------------------------------------
    def record_send(self, key: str, window_seconds: int, maximum: int = 0) -> None:
        """Count one request as sent (called before the HTTP call starts).

        ``maximum == 0`` means the model is unlimited, so there is nothing to
        measure: counting anyway would let a busy unlimited model grow its
        stamp list without bound (review M-3). The cooldown is still cleared --
        the model answered.
        """
        window = self._window(key)
        if maximum and maximum > 0:
            now = self._clock()
            self._prune(window, window_seconds, now)
            window.stamps.append(now)
            # A hard ceiling on one window's list, independent of ``maximum``:
            # a huge configured limit must not become a memory limit.
            if len(window.stamps) > MAX_STAMPS:
                del window.stamps[: len(window.stamps) - MAX_STAMPS]
        # A successful send resets the cooldown: the model answered.
        window.failed_at = None
        window.last_error = None

    def record_failure(self, key: str, message: str) -> None:
        window = self._window(key)
        window.failed_at = self._clock()
        window.last_error = (message or "")[:500]

    def record_success(self, key: str) -> None:
        window = self._entries.get(key)
        if window is not None:
            window.failed_at = None
            window.last_error = None

    def snapshot(self) -> dict[str, dict]:
        """Read-only view for the status endpoint (bounded by ``max_entries``)."""
        now = self._clock()
        result: dict[str, dict] = {}
        for key, window in list(self._entries.items()):
            result[key] = {
                "recentSends": len(window.stamps),
                "secondsSinceFailure": (round(now - window.failed_at, 1)
                                        if window.failed_at is not None else None),
                "lastError": window.last_error,
            }
        return result

    def clear(self) -> None:
        self._entries.clear()
