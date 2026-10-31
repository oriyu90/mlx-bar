"""Bounded off-loop disk reads (v3.3.3 / TASK-005).

v3.3.1 moved model-metadata reads off the event loop with
``asyncio.to_thread`` so a slow external volume could not freeze the whole
coordinator. That fixed the freeze but introduced an unbounded wait: the
thread can block in ``open()`` forever.

On macOS that is not hypothetical. When an app reads an external volume
without the *Removable Volumes* privacy grant, TCC raises a permission prompt
-- and a process launched by launchd has no window to show it in, so the
``open()`` simply never returns. The observed symptom was a resident preload
stuck at "running" with no worker process, no error and a perfectly
responsive app, which read as "launchd refuses to start anything".

Bounding the read turns that into a reported failure: the preload records
``failed`` with ``MODEL_VOLUME_UNREADABLE`` and the menu bar explains it.

A timed-out thread cannot be cancelled, so the guard remembers the stall for a
cooldown and fails fast instead of spawning another blocked thread per retry.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, TypeVar

from .errors import MLXBarError

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0
COOLDOWN_SECONDS = 60.0

CODE = "MODEL_VOLUME_UNREADABLE"
MESSAGE = (
    "モデルフォルダを読み取れませんでした。外付けボリュームへのアクセスが許可されていない"
    "可能性があります。システム設定 › プライバシーとセキュリティ › リムーバブルボリューム"
    "（またはフルディスクアクセス）で MLXBar を許可してから、もう一度お試しください"
)

T = TypeVar("T")


class VolumeGuard:
    """Runs blocking reads in a thread, with a deadline and a stall memory."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 cooldown: float = COOLDOWN_SECONDS) -> None:
        self.timeout = float(timeout)
        self.cooldown = float(cooldown)
        self._stalled_until = 0.0
        self._stall_reason = ""

    def stalled(self) -> bool:
        return time.monotonic() < self._stalled_until

    def stall_reason(self) -> str:
        return self._stall_reason if self.stalled() else ""

    def reset(self) -> None:
        self._stalled_until = 0.0
        self._stall_reason = ""

    async def read(self, func: Callable[..., T], *args, what: str = "model metadata") -> T:
        """Call ``func(*args)`` in a thread, bounded by ``self.timeout``.

        Raises ``MLXBarError(MODEL_VOLUME_UNREADABLE)`` when the volume does not
        answer. While a stall is remembered, further reads fail immediately --
        each abandoned read pins a thread that cannot be cancelled.
        """
        if self.stalled():
            raise MLXBarError(CODE, MESSAGE, 503, False)
        try:
            return await asyncio.wait_for(asyncio.to_thread(func, *args),
                                          timeout=self.timeout)
        except asyncio.TimeoutError:
            self._stalled_until = time.monotonic() + self.cooldown
            self._stall_reason = what
            LOGGER.warning(
                "Reading %s did not finish within %.0fs; treating the volume as "
                "unreachable for %.0fs (macOS may be waiting on a Removable "
                "Volumes permission prompt)", what, self.timeout, self.cooldown)
            raise MLXBarError(CODE, MESSAGE, 503, False) from None
