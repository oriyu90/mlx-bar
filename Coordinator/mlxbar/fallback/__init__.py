"""API fallback: hand a request to an external AI provider when MLXBar cannot answer.

v5.0.0. The package is deliberately split so the decision logic is pure and
testable without a running server:

* ``config``     -- defaults, normalisation and validation of the stored shape
* ``selector``   -- which external model a given request may be sent to, in order
* ``limiter``    -- per-model rate windows and failure cooldowns (bounded memory)
* ``translator`` -- internal messages/options -> each provider's request body
* ``client``     -- the HTTP/SSE side, normalised back into worker events
* ``service``    -- the orchestration the API surfaces call

Everything here is dormant unless ``apiFallback.enabled`` is true. A request
that never enables it takes the exact v4.0.2 path.
"""

from __future__ import annotations

from .config import (
    DEFAULT_PROVIDER,
    PROTOCOLS,
    FallbackSettings,
    normalise_settings,
    validate_settings,
)
from .selector import Candidate, select_candidates
from .limiter import FallbackLimiter
from .service import FallbackService, FallbackOutcome

__all__ = [
    "DEFAULT_PROVIDER",
    "PROTOCOLS",
    "FallbackSettings",
    "normalise_settings",
    "validate_settings",
    "Candidate",
    "select_candidates",
    "FallbackLimiter",
    "FallbackService",
    "FallbackOutcome",
]
