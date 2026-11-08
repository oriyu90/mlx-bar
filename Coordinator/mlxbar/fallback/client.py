"""HTTP/SSE side of the fallback: send a request, yield worker-shaped events.

The output is deliberately the **same event vocabulary the worker pool emits**
(``delta`` / ``reasoning_delta`` / ``tool_calls`` / ``completed`` / ``usage`` /
``error``) so the OpenAI and Anthropic surfaces can stream an external answer
through their existing encoders without a single change to those encoders.
That is the compatibility hinge of this feature: nothing downstream can tell an
external answer from a local one except the ``model`` name.

Failure policy: every network/protocol problem becomes an ``error`` event with a
``FALLBACK_*`` code and ``retryable`` set appropriately; the caller decides
whether to try the next candidate. ``asyncio.CancelledError`` is always
re-raised -- a disconnected client must not be retried elsewhere.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

import httpx

from . import translator
from .selector import Candidate

#: Cap on one SSE line / one tool-call argument buffer, so a hostile or broken
#: server cannot grow coordinator memory without bound.
MAX_BUFFER_BYTES = 1_048_576
CONNECT_TIMEOUT_SECONDS = 10


class FallbackError(Exception):
    """A failed external attempt, with the code the API surface should report."""

    def __init__(self, code: str, message: str, *, retryable: bool = True,
                 status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status

    def as_event(self) -> dict:
        return {"type": "error", "code": self.code, "message": self.message,
                "retryable": self.retryable}


def _error_from_status(status: int, body: str) -> FallbackError:
    snippet = (body or "").strip()
    if len(snippet) > 400:
        snippet = snippet[:400] + "…"
    if status in {401, 403}:
        return FallbackError("FALLBACK_AUTH_FAILED",
                             f"外部プロバイダーが認証を拒否しました（HTTP {status}）", retryable=False,
                             status=status)
    if status == 429:
        return FallbackError("FALLBACK_RATE_LIMITED",
                             f"外部プロバイダーのレート上限に達しました（HTTP 429）{snippet}",
                             retryable=True, status=status)
    if status in {400, 404, 422}:
        return FallbackError("FALLBACK_REJECTED",
                             f"外部プロバイダーがリクエストを受け付けませんでした（HTTP {status}）"
                             f"{snippet}", retryable=False, status=status)
    return FallbackError("FALLBACK_UPSTREAM_ERROR",
                         f"外部プロバイダーがエラーを返しました（HTTP {status}）{snippet}",
                         retryable=True, status=status)


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)
        if isinstance(error, str):
            return error
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return ""


# ---------------------------------------------------------------------------
# stream parsers: one per protocol, each yielding normalised events
# ---------------------------------------------------------------------------
def _openai_chat_events(payload: dict) -> list[dict]:
    events: list[dict] = []
    usage = payload.get("usage")
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            events.append({"type": "reasoning_delta", "text": reasoning})
        text = delta.get("content")
        if isinstance(text, str) and text:
            events.append({"type": "delta", "text": text})
        calls = delta.get("tool_calls")
        if isinstance(calls, list) and calls:
            events.append({"type": "tool_call_delta", "calls": calls})
        finish = choice.get("finish_reason")
        if finish:
            events.append({"type": "completed", "finish_reason": finish})
    if isinstance(usage, dict):
        events.append({"type": "usage",
                       "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                       "completion_tokens": int(usage.get("completion_tokens") or 0)})
    return events


def _anthropic_events(payload: dict) -> list[dict]:
    kind = payload.get("type")
    events: list[dict] = []
    if kind == "content_block_delta":
        delta = payload.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            events.append({"type": "delta", "text": delta["text"]})
        elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
            events.append({"type": "reasoning_delta", "text": delta["thinking"]})
        elif delta.get("type") == "input_json_delta":
            events.append({"type": "tool_call_delta", "calls": [{
                "index": int(payload.get("index") or 0),
                "function": {"arguments": delta.get("partial_json") or ""}}]})
    elif kind == "content_block_start":
        block = payload.get("content_block") or {}
        if block.get("type") == "tool_use":
            events.append({"type": "tool_call_delta", "calls": [{
                "index": int(payload.get("index") or 0),
                "id": block.get("id"),
                "type": "function",
                "function": {"name": block.get("name") or "", "arguments": ""}}]})
    elif kind == "message_delta":
        delta = payload.get("delta") or {}
        stop = delta.get("stop_reason")
        if stop:
            events.append({"type": "completed",
                           "finish_reason": {"end_turn": "stop", "max_tokens": "length",
                                             "tool_use": "tool_calls",
                                             "stop_sequence": "stop"}.get(stop, "stop")})
        usage = payload.get("usage") or {}
        if usage:
            events.append({"type": "usage",
                           "prompt_tokens": int(usage.get("input_tokens") or 0),
                           "completion_tokens": int(usage.get("output_tokens") or 0)})
    elif kind == "message_start":
        usage = (payload.get("message") or {}).get("usage") or {}
        if usage:
            events.append({"type": "usage",
                           "prompt_tokens": int(usage.get("input_tokens") or 0),
                           "completion_tokens": int(usage.get("output_tokens") or 0)})
    elif kind == "error":
        message = _extract_error_message(payload) or "外部プロバイダーがエラーを返しました"
        events.append({"type": "error", "code": "FALLBACK_UPSTREAM_ERROR",
                       "message": message, "retryable": True})
    return events


def _responses_events(payload: dict) -> list[dict]:
    kind = payload.get("type")
    events: list[dict] = []
    if kind == "response.output_text.delta" and payload.get("delta"):
        events.append({"type": "delta", "text": str(payload["delta"])})
    elif kind in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"} \
            and payload.get("delta"):
        events.append({"type": "reasoning_delta", "text": str(payload["delta"])})
    elif kind == "response.output_item.added":
        item = payload.get("item") or {}
        if item.get("type") == "function_call":
            events.append({"type": "tool_call_delta", "calls": [{
                "index": int(payload.get("output_index") or 0),
                "id": item.get("call_id") or item.get("id"),
                "type": "function",
                "function": {"name": item.get("name") or "", "arguments": ""}}]})
    elif kind == "response.function_call_arguments.delta":
        events.append({"type": "tool_call_delta", "calls": [{
            "index": int(payload.get("output_index") or 0),
            "function": {"arguments": str(payload.get("delta") or "")}}]})
    elif kind in {"response.completed", "response.incomplete"}:
        response = payload.get("response") or {}
        usage = response.get("usage") or {}
        if usage:
            events.append({"type": "usage",
                           "prompt_tokens": int(usage.get("input_tokens") or 0),
                           "completion_tokens": int(usage.get("output_tokens") or 0)})
        status = response.get("status")
        events.append({"type": "completed",
                       "finish_reason": "length" if status == "incomplete" else "stop"})
    elif kind in {"response.failed", "error"}:
        message = (_extract_error_message(payload.get("response") or {})
                   or _extract_error_message(payload)
                   or "外部プロバイダーがエラーを返しました")
        events.append({"type": "error", "code": "FALLBACK_UPSTREAM_ERROR",
                       "message": message, "retryable": True})
    return events


def parse_event(protocol: str, payload: dict) -> list[dict]:
    """Decode one SSE data frame into normalised events.

    A frame carrying a top-level ``error`` is an error for *every* protocol --
    OpenAI-compatible servers emit ``{"error": {...}}`` mid-stream, and some do
    so before the first token. Recognising it here (rather than only in the
    protocol-specific branches) is what stops such a failure from being read as
    an empty success.
    """
    if isinstance(payload.get("error"), (dict, str)) and payload.get("type") != "error":
        return [{"type": "error", "code": "FALLBACK_UPSTREAM_ERROR",
                 "message": _extract_error_message(payload) or "外部プロバイダーがエラーを返しました",
                 "retryable": True}]
    if protocol == "anthropic-messages":
        return _anthropic_events(payload)
    if protocol == "openai-responses":
        return _responses_events(payload)
    return _openai_chat_events(payload)


def non_stream_events(protocol: str, payload: dict) -> list[dict]:
    """Whole-body response -> the same normalised event list."""
    events: list[dict] = []
    if protocol == "anthropic-messages":
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                events.append({"type": "delta", "text": block["text"]})
            elif block.get("type") == "thinking" and block.get("thinking"):
                events.append({"type": "reasoning_delta", "text": block["thinking"]})
            elif block.get("type") == "tool_use":
                events.append({"type": "tool_call_delta", "calls": [{
                    "index": len([e for e in events if e["type"] == "tool_call_delta"]),
                    "id": block.get("id"),
                    "type": "function",
                    "function": {"name": block.get("name") or "",
                                 "arguments": json.dumps(block.get("input") or {},
                                                         ensure_ascii=False)}}]})
        usage = payload.get("usage") or {}
        if usage:
            events.append({"type": "usage",
                           "prompt_tokens": int(usage.get("input_tokens") or 0),
                           "completion_tokens": int(usage.get("output_tokens") or 0)})
        stop = payload.get("stop_reason")
        events.append({"type": "completed",
                       "finish_reason": {"end_turn": "stop", "max_tokens": "length",
                                         "tool_use": "tool_calls"}.get(stop, "stop")})
        return events
    if protocol == "openai-responses":
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text" \
                            and part.get("text"):
                        events.append({"type": "delta", "text": part["text"]})
            elif item.get("type") == "function_call":
                events.append({"type": "tool_call_delta", "calls": [{
                    "index": len([e for e in events if e["type"] == "tool_call_delta"]),
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {"name": item.get("name") or "",
                                 "arguments": item.get("arguments") or "{}"}}]})
        usage = payload.get("usage") or {}
        if usage:
            events.append({"type": "usage",
                           "prompt_tokens": int(usage.get("input_tokens") or 0),
                           "completion_tokens": int(usage.get("output_tokens") or 0)})
        events.append({"type": "completed",
                       "finish_reason": "length" if payload.get("status") == "incomplete" else "stop"})
        return events
    # OpenAI Chat Completions: `choices[].message` instead of `delta`.
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            events.append({"type": "reasoning_delta", "text": reasoning})
        text = message.get("content")
        if isinstance(text, str) and text:
            events.append({"type": "delta", "text": text})
        for index, call in enumerate(message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            events.append({"type": "tool_call_delta", "calls": [{
                "index": index, "id": call.get("id"), "type": "function",
                "function": call.get("function") or {}}]})
        finish = choice.get("finish_reason")
        if finish:
            events.append({"type": "completed", "finish_reason": finish})
    usage = payload.get("usage")
    if isinstance(usage, dict):
        events.append({"type": "usage",
                       "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                       "completion_tokens": int(usage.get("completion_tokens") or 0)})
    return events


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
async def _iter_sse(response: httpx.Response) -> AsyncIterator[dict]:
    """Yield decoded JSON payloads from an SSE body, bounded in memory."""
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        if len(buffer) > MAX_BUFFER_BYTES and "\n" not in buffer:
            raise FallbackError("FALLBACK_PROTOCOL_ERROR",
                                "外部プロバイダーの応答が大きすぎるため打ち切りました")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip("\r")
            if not line or line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
    if buffer.strip().startswith("data:"):
        data = buffer.strip()[5:].strip()
        if data and data != "[DONE]":
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                yield payload


async def stream_candidate(
    candidate: Candidate,
    messages: list[dict],
    images: list[str],
    options: dict,
    *,
    api_key: str | None,
    request_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[dict]:
    """Send one request to ``candidate`` and yield worker-shaped events.

    Raises ``FallbackError`` for a failure that happened *before* any content
    was produced (so the caller can try the next candidate). Once content has
    been yielded, failures are reported as an ``error`` event instead, because
    the upstream has already sent bytes to the client.
    """
    protocol = candidate.protocol
    body = translator.build_body(protocol, candidate.model, messages, images, options, True)
    url = candidate.base_url + translator.endpoint_path(protocol)
    headers = translator.auth_headers(protocol, api_key, candidate.model.get("extraHeaders"))
    timeout = httpx.Timeout(candidate.timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)
    started = time.monotonic()
    produced = False
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport,
                                     follow_redirects=False) as client:
            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    raise _error_from_status(response.status_code, text)
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    # Some compatible servers ignore stream=true; accept a whole
                    # JSON body rather than failing a request that succeeded.
                    raw = (await response.aread()).decode("utf-8", "replace")
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise FallbackError("FALLBACK_PROTOCOL_ERROR",
                                            "外部プロバイダーの応答を解釈できません") from exc
                    if isinstance(payload, dict) and (
                            payload.get("error") or payload.get("status") == "failed"):
                        raise FallbackError("FALLBACK_UPSTREAM_ERROR",
                                            _extract_error_message(payload)
                                            or _extract_error_message(payload.get("response") or {})
                                            or "外部プロバイダーがエラーを返しました")
                    body_events = non_stream_events(protocol, payload)
                    for event in body_events:
                        if event["type"] in {"delta", "reasoning_delta", "tool_call_delta"}:
                            produced = True
                        yield event
                    if not any(event["type"] in {"delta", "reasoning_delta",
                                                 "tool_call_delta", "completed", "usage"}
                               for event in body_events):
                        raise FallbackError("FALLBACK_PROTOCOL_ERROR",
                                            "外部プロバイダーから内容が返りませんでした")
                    yield {"type": "metrics", "fallback": True,
                           "duration_ms": round((time.monotonic() - started) * 1000)}
                    return
                completed = False
                saw_usage = False
                tool_argument_bytes = 0
                async for payload in _iter_sse(response):
                    for event in parse_event(protocol, payload):
                        if event["type"] == "delta" and not event.get("text"):
                            continue
                        if event["type"] in {"delta", "reasoning_delta", "tool_call_delta"}:
                            produced = True
                            if event["type"] == "tool_call_delta":
                                # D-4: a tool-call argument buffer is bounded
                                # too -- a broken server must not grow it.
                                for call in event.get("calls") or []:
                                    function = (call or {}).get("function") or {}
                                    arguments = function.get("arguments")
                                    if isinstance(arguments, str):
                                        tool_argument_bytes += len(arguments)
                                if tool_argument_bytes > MAX_BUFFER_BYTES:
                                    raise FallbackError(
                                        "FALLBACK_PROTOCOL_ERROR",
                                        "外部プロバイダーのtool call引数が大きすぎるため打ち切りました")
                        elif event["type"] == "completed":
                            completed = True
                        elif event["type"] == "usage":
                            saw_usage = True
                        elif event["type"] == "error":
                            # An upstream that reports a failure *as an SSE
                            # event* (Anthropic `event: error`, Responses
                            # `response.failed`, a chat `{"error": ...}` frame)
                            # must never be swallowed into an empty success.
                            # Before any content, raise so the next candidate is
                            # tried; after content, the event is yielded and the
                            # caller reports it (the client already has bytes).
                            if produced:
                                yield event
                                return
                            raise FallbackError(event.get("code") or "FALLBACK_UPSTREAM_ERROR",
                                                event.get("message") or "外部プロバイダーがエラーを返しました")
                        yield event
                if not produced and not completed and not saw_usage:
                    # M-1: an empty stream (no content, no finish, no usage) is
                    # a broken upstream, not an empty answer. Raising lets the
                    # next candidate answer instead of returning "" as success.
                    raise FallbackError("FALLBACK_PROTOCOL_ERROR",
                                        "外部プロバイダーから内容が返りませんでした")
                if not completed:
                    yield {"type": "completed", "finish_reason": "stop"}
                yield {"type": "metrics", "fallback": True,
                       "duration_ms": round((time.monotonic() - started) * 1000)}
    except FallbackError:
        raise
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException as exc:
        if produced:
            yield {"type": "error", "code": "FALLBACK_TIMEOUT",
                   "message": "外部プロバイダーの応答がタイムアウトしました", "retryable": True}
            return
        raise FallbackError("FALLBACK_TIMEOUT",
                            "外部プロバイダーの応答がタイムアウトしました") from exc
    except httpx.HTTPError as exc:
        if produced:
            yield {"type": "error", "code": "FALLBACK_NETWORK_ERROR",
                   "message": f"外部プロバイダーに接続できません: {exc}", "retryable": True}
            return
        raise FallbackError("FALLBACK_NETWORK_ERROR",
                            f"外部プロバイダーに接続できません: {exc}") from exc


async def probe_provider(candidate: Candidate, *, api_key: str | None,
                         transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """A cheap liveness check for the Settings page's "test" button.

    Sends a one-token request rather than listing models: ``/models`` is not
    part of the Responses or Anthropic protocols, and a real request proves the
    key, the URL and the model name at once.
    """
    messages = [{"role": "user", "content": "ping"}]
    options = {"max_tokens": 1, "temperature": 0}
    started = time.monotonic()
    text = ""
    try:
        async for event in stream_candidate(candidate, messages, [], options,
                                            api_key=api_key, transport=transport):
            if event.get("type") == "delta":
                text += event.get("text") or ""
            elif event.get("type") == "error":
                raise FallbackError(event.get("code") or "FALLBACK_UPSTREAM_ERROR",
                                    event.get("message") or "外部プロバイダーがエラーを返しました")
    except FallbackError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message,
                "durationMs": round((time.monotonic() - started) * 1000)}
    return {"ok": True, "durationMs": round((time.monotonic() - started) * 1000),
            "sample": text[:80]}
