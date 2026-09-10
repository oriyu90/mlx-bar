"""Adaptive Memory Planner -- the coordinator (tier 1) entry point.

Mirrors ``context_compression.maybe_compress_messages``: best-effort, never
raises, returns the input unchanged on any doubt. Reuses that module's tail
splitter, size helper and summarizer so the two features stay consistent about
what "the verbatim tail" and "a summary" mean.
"""

from __future__ import annotations

import json
import logging
import time

from ..context_compression import (
    _chars,
    _effective_max_prompt_characters,
    _split_point,
    _summarize,
)
from . import segment
from .latent_store import NoteCache, note_key
from .metrics import AdaptiveMetrics
from .policy import POLICY_VERSION, label_messages

LOGGER = logging.getLogger(__name__)

NOTE_PREFIX = "[Earlier discussion condensed]\n"
_MIN_SHRINK = 0.95  # rebuilt prompt must be at least 5% smaller to be worth it

# Worker-tier knobs forwarded verbatim in the generation ``options`` so the
# mlx-lm worker can attempt the soft-token Hybrid Prefiller. Kept to plain
# JSON-safe scalars.
_WORKER_KEYS = (
    "softToken", "policy", "keepTailMessages", "maxLatentTokens",
    "maxRetrievedSegments", "verbatimProtection", "memoryPressureRatio",
    "persistentLatentCache", "persistentHybridKV", "fallbackToExact",
)


def adaptive_memory_worker_options(settings) -> dict | None:
    """The ``adaptiveMemory`` sub-dict passed through to the worker, or None."""
    config = ((settings.data or {}).get("experimental", {}) or {}).get("adaptiveMemory", {}) or {}
    if not config.get("enabled", False):
        return None
    options = {key: config[key] for key in _WORKER_KEYS if key in config}
    options["enabled"] = True
    options["policyVersion"] = POLICY_VERSION
    return options


def _model_fingerprint(loaded: dict | None) -> str:
    loaded = loaded or {}
    capabilities = loaded.get("capabilities") or {}
    return "|".join(str(part) for part in (
        loaded.get("id") or loaded.get("name") or "",
        capabilities.get("runtimeVersion") or capabilities.get("engine") or "",
    ))


async def maybe_plan_adaptive_memory(workers, loaded, messages, tools, settings,
                                     request_id) -> tuple[list[dict], dict | None]:
    """Best-effort segment-level planning. Never raises."""
    try:
        return await _plan(workers, loaded, messages, tools, settings, request_id)
    except Exception as exc:  # noqa: BLE001 - experimental path must never break a request
        LOGGER.warning("adaptive memory planning skipped (%s); using the raw prompt", exc)
        return messages, None


async def _plan(workers, loaded, messages, tools, settings, request_id):
    config = ((settings.data or {}).get("experimental", {}) or {}).get("adaptiveMemory", {}) or {}
    if not config.get("enabled", False):
        return messages, None
    # Defence in depth: the validator already forbids both being on.
    if (settings.data or {}).get("contextCompression", {}).get("enabled", False):
        return messages, None
    if not isinstance(messages, list) or len(messages) < 4:
        return messages, None

    limit = int(_effective_max_prompt_characters(loaded, settings))
    if limit <= 0:
        return messages, None
    raw_chars = _chars(messages) + _chars(tools)
    trigger_ratio = float(config.get("triggerRatio", 0.60))
    if raw_chars < limit * trigger_ratio:
        return messages, None

    split = _split_point(messages, int(config.get("keepTailMessages", 8)))
    if split is None:
        return messages, None
    start, tail_start = split
    head, middle, tail = messages[:start], messages[start:tail_start], messages[tail_start:]
    if not middle:
        return messages, None

    policy = str(config.get("policy", "balanced"))
    verbatim_protection = bool(config.get("verbatimProtection", True))
    metrics = AdaptiveMetrics(raw_prompt_chars=raw_chars, policy=policy,
                              policy_version=POLICY_VERSION,
                              soft_token_requested=bool(config.get("softToken", False)))

    policy_started = time.monotonic()
    labels = label_messages(middle, policy=policy, verbatim_protection=verbatim_protection)
    metrics.policy_ms = round((time.monotonic() - policy_started) * 1000, 2)
    if "latent" not in labels and "cold" not in labels:
        return messages, None

    root = getattr(settings, "root", None) if config.get("persistentLatentCache", True) else None
    cache = NoteCache(root)
    fingerprint = _model_fingerprint(loaded)
    model_id = str((loaded or {}).get("id", "")) or None
    note_max_tokens = max(128, min(512, int(config.get("maxLatentTokens", 256))))

    rebuilt: list[dict] = []
    run: list[dict] = []
    note_index = 0
    writer_ms = 0.0

    async def flush_run() -> None:
        nonlocal note_index, writer_ms
        if not run:
            return
        canonical = json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = note_key(canonical, "latent-run", fingerprint, POLICY_VERSION)
        note = cache.get(key)
        if note is None:
            metrics.latent_cache_misses += 1
            started = time.monotonic()
            note = (await _summarize(workers, model_id, list(run), note_max_tokens,
                                     f"{request_id}-amnote{note_index}")).strip()
            writer_ms += (time.monotonic() - started) * 1000
            if note:
                cache.put(key, note)
        else:
            metrics.latent_cache_hits += 1
        note_index += 1
        if note:
            rebuilt.append({"role": "system", "content": NOTE_PREFIX + note})
            metrics.latent_notes += 1
        else:
            # Summarizer produced nothing: keep the run verbatim rather than
            # silently dropping it.
            rebuilt.extend(run)
            metrics.exact_messages += len(run)
        run.clear()

    for message, label in zip(middle, labels):
        if label == "latent":
            run.append(message)
            continue
        await flush_run()
        if label == "cold":
            metrics.cold_dropped += 1
            metrics.cold_source_chars += _chars(message)
        else:
            rebuilt.append(message)
            metrics.exact_messages += 1
    await flush_run()

    metrics.writer_ms = round(writer_ms, 2)
    compressed = head + rebuilt + tail
    compressed_chars = _chars(compressed) + _chars(tools)
    if compressed_chars >= raw_chars * _MIN_SHRINK:
        return messages, None
    metrics.effective_prompt_chars = compressed_chars
    metrics.compression_ratio = round(compressed_chars / max(1, raw_chars), 4)

    summary = metrics.as_summary()
    summary["triggerRatio"] = trigger_ratio
    return compressed, summary
