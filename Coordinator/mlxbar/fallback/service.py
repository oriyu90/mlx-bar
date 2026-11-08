"""Orchestration for API fallback: decide, try candidates, report.

The two API surfaces (OpenAI Chat Completions and Anthropic Messages) call
``FallbackService`` at exactly the point where the local path refuses a request
(queue full, rate limit, or a local error). Everything else -- which model,
which protocol, rate windows, cooldowns, trimming, retrying the next candidate
-- lives here so the surfaces stay thin and their existing encoders stay
untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from ..errors import MLXBarError
from . import translator
from .client import FallbackError, stream_candidate
from .config import FallbackSettings
from .limiter import FallbackLimiter
from .selector import Candidate, select_candidates

LOGGER = logging.getLogger(__name__)

#: Local errors that mean "MLXBar cannot answer this request right now" rather
#: than "the request is wrong". Only these trigger a fallback; MODEL_NOT_FOUND
#: and INVALID_REQUEST never do, because a typo must stay visible.
FALLBACK_TRIGGER_CODES = frozenset({
    "QUEUE_FULL",
    "QUEUE_TIMEOUT",
    "ENGINE_BUSY",
    "MODEL_NOT_LOADED",
    "MODEL_LOAD_FAILED",
    "MODEL_LOAD_DEGRADED",
    "MODEL_VOLUME_UNREADABLE",
    "MODEL_VOLUME_DETACHED",
    "RESIDENT_LIMIT_REACHED",
    "MEMORY_PRESSURE",
    "MEMORY_BUDGET_EXCEEDED",
    "MEMORY_HEADROOM_INSUFFICIENT",
    "MEMORY_BUDGET_TOTAL",
    "MEMORY_POOL_EXHAUSTED",
    "MEMORY_POSTLOAD_EXCEEDED",
    "GENERATION_FAILED",
    "MODEL_CRASH_LOOP",
    "ENGINE_SHUTTING_DOWN",
    "RUNTIME_MEMORY_LIMIT_UNAVAILABLE",
})


@dataclass
class FallbackOutcome:
    """What the service decided, for the API log and the status endpoint."""

    reason: str
    candidate: Candidate
    started_at: float = field(default_factory=time.time)
    attempts: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {"reason": self.reason, "provider": self.candidate.provider_name,
                "providerId": self.candidate.provider_id,
                "model": self.candidate.model_name, "protocol": self.candidate.protocol,
                "attempts": list(self.attempts), "at": self.started_at}


class FallbackService:
    """Owns the limiter and turns settings into an ordered candidate list."""

    def __init__(self, settings_store, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock=time.monotonic):
        self.settings = settings_store
        self.limiter = FallbackLimiter(clock=clock)
        # Injected by tests; None means a real client per attempt.
        self.transport = transport
        self.last_outcome: dict | None = None
        self.recent: list[dict] = []
        self._max_recent = 20

    # -- settings ---------------------------------------------------------
    def current(self) -> FallbackSettings:
        try:
            return FallbackSettings.from_document(self.settings.data.get("apiFallback"))
        except Exception:  # noqa: BLE001 - a broken document must not break requests
            LOGGER.warning("apiFallback could not be read; fallback disabled", exc_info=True)
            return FallbackSettings()

    def api_key(self, provider_id: str) -> str | None:
        getter = getattr(self.settings, "fallback_api_key", None)
        if not callable(getter):
            return None
        try:
            return getter(provider_id)
        except Exception:  # noqa: BLE001 - never fail a request on a key read
            return None

    # -- selection --------------------------------------------------------
    def plan(self, requested: str, *, has_images: bool,
             aliases: tuple[str, ...] = ()) -> list[Candidate]:
        settings = self.current()
        if not settings.enabled:
            return []
        cooling = [key for key in self.limiter.snapshot()
                   if self.limiter.is_cooling(key, settings.cooldown_seconds)]
        limited = []
        for provider in settings.active_providers():
            for model in provider.get("models") or []:
                if not model.get("enabled"):
                    continue
                key = f"{provider.get('id')}/{model.get('id')}"
                if self.limiter.is_limited(key, int(model.get("maxRequests") or 0),
                                           int(model.get("rateWindowSeconds") or 60)):
                    limited.append(key)
        return select_candidates(settings, requested, has_images=has_images,
                                 aliases=aliases, cooling=cooling, limited=limited,
                                 limit=settings.max_attempts)

    def should_forward_images(self) -> bool:
        settings = self.current()
        return settings.enabled and settings.forward_images_immediately \
            and settings.any_image_capable()

    def should_fallback(self, requested: str, *, has_images: bool,
                        aliases: tuple[str, ...] = ()) -> bool:
        return bool(self.plan(requested, has_images=has_images, aliases=aliases))

    # -- execution --------------------------------------------------------
    async def run(self, requested: str, messages: list[dict], images: list[str],
                  options: dict, *, reason: str,
                  aliases: tuple[str, ...] = ()) -> AsyncIterator[dict]:
        """Yield worker-shaped events from the first candidate that answers.

        Raises ``FallbackError`` when every candidate failed, so the caller can
        re-raise the original local error instead.
        """
        candidates = self.plan(requested, has_images=bool(images), aliases=aliases)
        if not candidates:
            raise FallbackError("FALLBACK_UNAVAILABLE",
                                "フォールバック先が設定されていません", retryable=False)
        settings = self.current()
        failures: list[dict] = []
        outcome: FallbackOutcome | None = None
        for candidate in candidates:
            window = int(candidate.model.get("rateWindowSeconds") or 60)
            trimmed, dropped = translator.trim_to_context(
                messages, images, int(candidate.model.get("maxContextTokens") or 0))
            self.limiter.record_send(candidate.key, window,
                                     int(candidate.model.get("maxRequests") or 0))
            attempt = {"provider": candidate.provider_name, "model": candidate.model_name,
                       "protocol": candidate.protocol, "droppedMessages": dropped}
            try:
                async for event in stream_candidate(
                        candidate, trimmed, images, options,
                        api_key=self.api_key(candidate.provider_id),
                        transport=self.transport):
                    if event.get("type") == "error":
                        self.limiter.record_failure(candidate.key, event.get("message") or "")
                    elif event.get("type") == "metrics":
                        self.limiter.record_success(candidate.key)
                    yield event
                attempt["ok"] = True
                failures.append(attempt)
                outcome = FallbackOutcome(reason=reason, candidate=candidate, attempts=failures)
                self._record(outcome)
                return
            except FallbackError as exc:
                self.limiter.record_failure(candidate.key, exc.message)
                attempt.update({"ok": False, "code": exc.code, "message": exc.message})
                failures.append(attempt)
                LOGGER.info("Fallback attempt failed: %s (%s): %s",
                            candidate.display, exc.code, exc.message)
                continue
        self._record_failure(reason, failures)
        raise FallbackError("FALLBACK_ALL_FAILED",
                            "すべてのフォールバック先が失敗しました",
                            retryable=any(item.get("code") != "FALLBACK_AUTH_FAILED"
                                          for item in failures))

    def _record(self, outcome: FallbackOutcome) -> None:
        summary = outcome.summary()
        self.last_outcome = summary
        self.recent = ([summary] + self.recent)[: self._max_recent]

    def _record_failure(self, reason: str, failures: list[dict]) -> None:
        summary = {"reason": reason, "provider": None, "model": None, "protocol": None,
                   "attempts": failures, "at": time.time(), "failed": True}
        self.last_outcome = summary
        self.recent = ([summary] + self.recent)[: self._max_recent]

    # -- diagnostics ------------------------------------------------------
    def status(self) -> dict:
        settings = self.current()
        providers = []
        for provider in settings.providers:
            models = []
            for model in provider.get("models") or []:
                key = f"{provider.get('id')}/{model.get('id')}"
                window = int(model.get("rateWindowSeconds") or 60)
                models.append({
                    "id": model.get("id"), "model": model.get("model"),
                    "enabled": bool(model.get("enabled")),
                    "priority": model.get("priority"),
                    "supportsImages": bool(model.get("supportsImages")),
                    "sourceModels": list(model.get("sourceModels") or []),
                    "maxRequests": model.get("maxRequests"),
                    "rateWindowSeconds": window,
                    "usedInWindow": self.limiter.usage(key, window),
                    "coolingDown": self.limiter.is_cooling(key, settings.cooldown_seconds),
                })
            providers.append({"id": provider.get("id"), "name": provider.get("name"),
                              "protocol": provider.get("protocol"),
                              "enabled": bool(provider.get("enabled")),
                              "models": models})
        return {"enabled": settings.enabled, "providers": providers,
                "lastOutcome": self.last_outcome, "recent": list(self.recent)}

    def reset_limits(self) -> None:
        self.limiter.clear()


def describe_attempts(exc: FallbackError) -> dict:
    """Additive detail attached to the original local error."""
    return {"fallback": {"code": exc.code, "message": exc.message,
                         "retryable": exc.retryable}}


def is_trigger(exc: BaseException) -> bool:
    return isinstance(exc, MLXBarError) and exc.code in FALLBACK_TRIGGER_CODES


async def close_quietly(generation) -> None:
    """Close an async generator without letting cleanup failure escape."""
    if generation is None:
        return
    try:
        await asyncio.shield(generation.aclose())
    except (asyncio.CancelledError, GeneratorExit):
        pass
    except Exception:  # noqa: BLE001
        pass
