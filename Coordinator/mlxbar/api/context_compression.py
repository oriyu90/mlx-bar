"""Transparent context compression for long-running agent conversations.

ZCode / Claude Code / OpenCode-style clients are stateless callers that resend
the *entire* conversation on every request. MLXBar's prompt cache (see
``Workers/mlx_lm_worker/prompt_cache.py``) already avoids re-prefilling that
history, but it does not shrink it: attention cost, the model's own context
window, and KV-cache memory all still scale with the total token count. A
long-running coding-agent session eventually gets slow (and, past
``effectiveMaxPromptCharacters``, outright rejected with ``INPUT_TOO_LARGE``)
even with a perfect cache hit.

This module summarizes the *middle* of an oversized conversation -- never the
leading system/tool-schema message, never the most recent messages, never a
``tool_calls``/``tool`` pairing split across the boundary, never an
image-bearing turn -- into one synthetic ``system`` message, using the same
already-loaded model. It is entirely best-effort: any failure (a bad
response, a timeout, a worker error) falls back to the original, uncompressed
messages. Losing the speed-up costs time; failing the request costs the
answer, exactly the trade-off ``prompt_cache.py`` already makes for its own
disk tier.

Disabled by default (``contextCompression.enabled`` = False): the summary is
never a byte-for-byte substitute for what the client actually sent, so
turning this on changes what the model can "see" of earlier turns. That is
an explicit, visible trade a user opts into, not a default behaviour change.
"""

from __future__ import annotations

import json
import logging

LOGGER = logging.getLogger(__name__)

SUMMARY_PREFIX = "[Earlier conversation summarized]\n"
MIN_MIDDLE_MESSAGES = 2

SUMMARIZE_INSTRUCTION = (
    "Summarize the conversation above into concise notes that preserve every "
    "fact, decision, file path, command, and pending task needed to continue "
    "the work without re-reading it. Write the summary in the same language "
    "as the conversation. Output only the notes -- no commentary, no "
    "acknowledgement of this instruction."
)


def _chars(value) -> int:
    if not value:
        return 0
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


def _has_image(message: dict) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(part, dict) and part.get("type") == "image_url" for part in content)


def _is_unresolved_tool_turn(message: dict) -> bool:
    return message.get("role") == "assistant" and bool(message.get("tool_calls"))


def _split_point(messages: list[dict], keep_tail: int) -> tuple[int, int] | None:
    """Return ``(start, tail_start)``: ``messages[start:tail_start]`` is the
    compressible middle; everything before ``start`` and from ``tail_start``
    onward is always sent verbatim.

    ``tail_start`` is walked left from the naive tail boundary until it no
    longer splits a ``tool_calls``/``tool`` pairing and no longer leaves an
    image-bearing message on the compressible side -- both would either
    produce an invalid message sequence for an OpenAI/Anthropic-compatible
    client, or silently drop an image the client still expects the model to
    have seen.
    """
    if not messages:
        return None
    start = 1 if messages[0].get("role") == "system" else 0
    if len(messages) - start <= keep_tail:
        return None
    tail_start = max(start, len(messages) - keep_tail)
    first_image = next((i for i in range(start, len(messages)) if _has_image(messages[i])), None)
    if first_image is not None and first_image < tail_start:
        tail_start = first_image
    while tail_start > start:
        candidate = messages[tail_start]
        previous = messages[tail_start - 1]
        if candidate.get("role") == "tool" or _is_unresolved_tool_turn(previous):
            tail_start -= 1
            continue
        break
    if tail_start - start < MIN_MIDDLE_MESSAGES:
        return None
    return start, tail_start


async def _summarize(workers, model_id: str | None, middle: list[dict],
                     summary_max_tokens: int, request_id: str) -> str:
    prompt_messages = middle + [{"role": "user", "content": SUMMARIZE_INSTRUCTION}]
    options = {"temperature": 0, "max_tokens": max(1, int(summary_max_tokens)),
               "tools": [], "tool_choice": "none", "parallel_tool_calls": False}
    generate_for_model = getattr(workers, "generate_for_model", None)
    summary_request_id = f"{request_id}-compact"
    generation = (generate_for_model(model_id, prompt_messages, [], options, summary_request_id)
                  if generate_for_model and model_id else
                  workers.generate(prompt_messages, [], options, summary_request_id))
    text = ""
    try:
        async for event in generation:
            if event.get("type") == "delta":
                text += event.get("text", "")
            elif event.get("type") == "error":
                raise RuntimeError(event.get("message") or event.get("code") or "summary generation failed")
    finally:
        await generation.aclose()
    return text


def _effective_max_prompt_characters(loaded: dict | None, settings) -> int:
    """Same formula as ``WorkerSupervisor.effective_max_prompt_characters``,
    computed directly from *this request's* resolved model rather than
    ``ModelPoolSupervisor.effective_max_prompt_characters()`` -- the latter
    always reflects the pool's primary slot, which is the wrong limit for a
    request targeting a different resident model in a multi-model pool.
    """
    configured = int(settings.data.get("generation", {}).get("maxPromptCharacters", 100000))
    capabilities = (loaded or {}).get("capabilities") or {}
    model_limit = capabilities.get("modelMaxTokens")
    if isinstance(model_limit, int) and model_limit > 0:
        return max(configured, min(model_limit * 4, 10_000_000))
    return configured


async def maybe_compress_messages(workers, loaded: dict | None, messages: list[dict],
                                  tools, settings, request_id: str) -> tuple[list[dict], dict | None]:
    """Best-effort compression. Never raises; on any doubt, returns the input unchanged."""
    config = (settings.data or {}).get("contextCompression", {})
    if not config.get("enabled", False):
        return messages, None
    try:
        limit = int(_effective_max_prompt_characters(loaded, settings))
    except Exception:
        return messages, None
    if limit <= 0:
        return messages, None
    model_id = str((loaded or {}).get("id", "")) or None
    original_chars = _chars(messages) + _chars(tools)
    trigger_ratio = float(config.get("triggerRatio", 0.7))
    if original_chars < limit * trigger_ratio:
        return messages, None
    split = _split_point(messages, int(config.get("keepTailMessages", 8)))
    if split is None:
        return messages, None
    start, tail_start = split
    head, middle, tail = messages[:start], messages[start:tail_start], messages[tail_start:]
    try:
        summary = await _summarize(workers, model_id, middle,
                                   config.get("summaryMaxTokens", 800), request_id)
    except Exception as exc:
        LOGGER.warning("Context compression summary failed; using the uncompressed prompt: %s", exc)
        return messages, None
    summary = summary.strip()
    if not summary:
        return messages, None
    synthetic = {"role": "system", "content": SUMMARY_PREFIX + summary}
    compressed = head + [synthetic] + tail
    compressed_chars = _chars(compressed) + _chars(tools)
    return compressed, {"originalChars": original_chars, "compressedChars": compressed_chars,
                        "droppedMessages": len(middle), "triggerRatio": trigger_ratio}
