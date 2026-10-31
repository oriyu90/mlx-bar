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
import os
import time
from typing import Callable, TypeVar

from .errors import MLXBarError

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20.0
COOLDOWN_SECONDS = 60.0

CODE = "MODEL_VOLUME_UNREADABLE"
#: The volume is simply not there (unmounted / renamed) -- a different problem
#: from a read that hangs, and the user fixes it differently.
DETACHED_CODE = "MODEL_VOLUME_DETACHED"
MESSAGE = (
    "モデルフォルダを読み取れませんでした。外付けボリュームへのアクセスが許可されていない"
    "可能性があります。システム設定 › プライバシーとセキュリティ › リムーバブルボリューム"
    "（またはフルディスクアクセス）で MLXBar を許可してから、もう一度お試しください"
)

DETACHED_MESSAGE = (
    "モデルフォルダが見つかりません。外付けボリュームが接続されているか確認してください"
)

#: How long the post-timeout existence probe may take. It only runs on the
#: failure path, and it is what tells "not mounted" from "not answering".
PRESENCE_PROBE_SECONDS = 2.0

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

    async def read(self, func: Callable[..., T], *args, what: str = "model metadata",
                   volume_path: str | None = None) -> T:
        """Call ``func(*args)`` in a thread, bounded by ``self.timeout``.

        Raises ``MLXBarError`` when the volume does not answer: ``DETACHED_CODE``
        if a short probe shows the path is simply absent, otherwise ``CODE``.
        While a stall is remembered, further reads fail immediately -- each
        abandoned read pins a thread that cannot be cancelled.

        v3.4.0: the error is now retryable and carries ``retry_after_ms``
        (matching the cooldown), so a client can back off instead of treating
        the failure as permanent -- the report's "60 seconds of 503 that looks
        like an auth failure".
        """
        if self.stalled():
            raise MLXBarError(CODE, MESSAGE, 503, True,
                              retry_after_ms=int(max(0.0, self._stalled_until
                                                     - time.monotonic()) * 1000))
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
            code, message = CODE, MESSAGE
            if volume_path is not None and await self._absent(volume_path):
                code, message = DETACHED_CODE, DETACHED_MESSAGE
            raise MLXBarError(code, message, 503, True,
                              retry_after_ms=int(self.cooldown * 1000)) from None

    async def _absent(self, path: str) -> bool:
        """Whether ``path`` is provably missing (False when it cannot be told).

        Only ever called after a read already timed out, and bounded itself: the
        same blocked volume can stall the probe too.
        """
        try:
            return not await asyncio.wait_for(
                asyncio.to_thread(os.path.exists, path), timeout=PRESENCE_PROBE_SECONDS)
        except (asyncio.TimeoutError, OSError, ValueError):
            return False
