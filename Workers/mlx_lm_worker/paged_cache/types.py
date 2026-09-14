from __future__ import annotations

from dataclasses import dataclass

FORMAT_VERSION = 1
BLOCK_SIZE = 256
MIN_REUSABLE_TOKENS = 64


class PagedCacheError(Exception):
    """A cache-only failure which must never fail generation."""


class LayoutMismatch(PagedCacheError):
    """Stored state is not compatible with the freshly-created runtime cache."""


class CorruptBlock(PagedCacheError):
    """A block failed content or metadata validation."""


class RestoreBudgetRejected(PagedCacheError):
    """The worst-case restore footprint cannot be admitted safely."""


@dataclass(frozen=True)
class CacheCapability:
    eligible: bool
    capability: str
    reason: str | None
    layout_fingerprint: str | None
    layers: int
    state_arity: int = 0
