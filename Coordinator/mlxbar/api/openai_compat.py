from __future__ import annotations

import json
import secrets
import time
import uuid
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..errors import MLXBarError
from .images import resolve_public_images


router = APIRouter()
SUPPORTED_ROLES = {"system", "developer", "user", "assistant", "tool"}
RESERVED_CHAT_TEMPLATE_KWARGS = {
    "add_generation_prompt", "num_images", "tokenize", "tool_choice", "tools",
}


def app_state(request: Request):
    return request.app.state.mlxbar


def authorize(request: Request) -> None:
    state = app_state(request)
    if not state.settings.data["api"].get("requireToken", True):
        return
    expected = "Bearer " + state.settings.api_token
    if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
        raise HTTPException(401, detail={"code": "AUTHENTICATION_FAILED"})


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/v1/models")
async def models(request: Request):
    authorize(request)
    state = app_state(request)
    loaded = state.workers.loaded
    data = [_model_descriptor(state, model, loaded) for model in state.database.list_models()]
    if loaded and not any(item["id"] in {loaded.get("id"), loaded.get("name")} for item in data):
        data.append(_model_descriptor(state, loaded, loaded))
    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id:path}")
async def retrieve_model(request: Request, model_id: str):
    authorize(request)
    state = app_state(request)
    model = _find_model(state, model_id)
    if not model:
        raise HTTPException(404, detail={"code": "MODEL_NOT_FOUND", "message": "指定されたモデルが見つかりません"})
    return _model_descriptor(state, model, state.workers.loaded)


@router.post("/v1/chat/completions")
async def chat(request: Request, body: dict):
    authorize(request)
    messages_for_log = body.get("messages")
    tools_for_log = body.get("tools")
    request.state.api_log = {
        "model": body.get("model") if isinstance(body.get("model"), str) else None,
        "stream": body.get("stream") if isinstance(body.get("stream"), bool) else False,
        "message_count": len(messages_for_log) if isinstance(messages_for_log, list) else 0,
        "tool_count": len(tools_for_log) if isinstance(tools_for_log, list) else 0,
        "message_chars": _json_size(messages_for_log),
        "tool_schema_chars": _json_size(tools_for_log),
    }
    # OpenAI-compatible clients routinely add provider-specific extension fields.
    # Unknown fields are intentionally ignored; known fields are validated below.
    if not isinstance(body.get("stream", False), bool):
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "stream must be a boolean", "param": "stream"})
    chat_template_kwargs = _normalize_chat_template_kwargs(body)
    stream_options = body.get("stream_options")
    if stream_options is not None:
        if not body.get("stream") or not isinstance(stream_options, dict):
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "stream_options requires stream=true", "param": "stream_options"})
        unknown_stream_options = set(stream_options) - {"include_usage", "include_obfuscation"}
        if unknown_stream_options or any(not isinstance(value, bool) for value in stream_options.values()):
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "Invalid stream_options", "param": "stream_options"})
    _reject_unimplemented(body)
    requested_model = body.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "modelを指定してください"})
    if body.get("n", 1) != 1:
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "nは1のみ対応しています"})
    modalities = body.get("modalities", ["text"])
    if modalities not in (None, ["text"]):
        raise HTTPException(400, detail={"code": "UNSUPPORTED_PARAMETER", "message": "text以外の出力には対応していません"})
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "messagesは1件以上の配列で指定してください"})
    normalized_messages, images = [], []
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "messageはオブジェクトで指定してください"})
        role = message.get("role")
        if role not in SUPPORTED_ROLES:
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "message.roleが不正です"})
        normalized = {key: message[key] for key in ("role", "name", "tool_call_id") if key in message}
        if "tool_calls" in message:
            if role != "assistant" or not isinstance(message["tool_calls"], list):
                raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "assistant.tool_callsが不正です"})
            normalized["tool_calls"] = _normalize_input_tool_calls(message["tool_calls"])
        content = message.get("content")
        if content is None and role == "assistant" and normalized.get("tool_calls"):
            normalized["content"] = None
        if isinstance(content, str):
            normalized["content"] = content
        elif isinstance(content, list):
            normalized_parts = []
            for part in content:
                if not isinstance(part, dict):
                    raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "content要素が不正です"})
                if part.get("type") == "text":
                    normalized_parts.append({"type": "text", "text": str(part.get("text", ""))})
                elif part.get("type") == "image_url":
                    image_url = part.get("image_url")
                    if not isinstance(image_url, dict) or not isinstance(image_url.get("url"), str):
                        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "image_urlが不正です"})
                    images.append(image_url["url"])
                    normalized_parts.append(part)
                else:
                    raise HTTPException(400, detail={"code": "UNSUPPORTED_PARAMETER"})
            normalized["content"] = normalized_parts
        elif content is None and role in {"tool", "user", "system", "developer"}:
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "contentが必要です"})
        elif content is not None:
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "contentは文字列、配列、またはnullで指定してください"})
        normalized_messages.append(normalized)
    tools = _normalize_tools(body.get("tools"))
    tool_choice = _normalize_tool_choice(body.get("tool_choice"), tools)
    if tool_choice == "required" and not tools:
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "tool_choice=required needs at least one tool", "param": "tool_choice"})
    effective_tools = [] if tool_choice == "none" else tools
    if isinstance(tool_choice, dict):
        selected_name = tool_choice["function"]["name"]
        effective_tools = [tool for tool in tools if tool["function"]["name"] == selected_name]
        if not effective_tools:
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "tool_choiceのfunctionがtoolsにありません"})
    request_id = "chatcmpl-" + uuid.uuid4().hex
    request.state.api_log.update({"request_id": request_id, "model": body.get("model"),
                                  "stream": bool(body.get("stream")),
                                  "message_count": len(messages), "tool_count": len(tools)})
    max_tokens = body.get("max_completion_tokens", body.get("max_tokens", 512))
    if max_tokens is None:
        max_tokens = 512
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "max_tokens must be a positive integer", "param": "max_tokens"})
    generation_defaults = app_state(request).settings.data.get("generation", {})
    options = {"temperature": body.get("temperature", generation_defaults.get("defaultTemperature", 0.7)),
               "top_p": body.get("top_p", generation_defaults.get("defaultTopP", 1.0)),
               "repetition_penalty": body.get("repetition_penalty",
                                               generation_defaults.get("defaultRepetitionPenalty", 1.0)),
               "repetition_context_size": body.get("repetition_context_size",
                                                     generation_defaults.get("repetitionContextSize", 20)),
               "max_tokens": max_tokens,
               "tools": effective_tools, "tool_choice": tool_choice,
               "parallel_tool_calls": body.get("parallel_tool_calls", True)}
    if chat_template_kwargs:
        options["chat_template_kwargs"] = chat_template_kwargs
    request.state.api_log.update({"max_tokens": max_tokens,
                                  "reasoning_mode": _reasoning_mode(chat_template_kwargs)})
    for key in ("frequency_penalty", "presence_penalty", "seed", "stop"):
        if key in body:
            options[key] = body[key]
    # Image references from this listener are untrusted: rewrite them into a
    # private directory so a caller can never name a path or URL of its own.
    # Done before the model is resolved so a rejected reference cannot trigger
    # an expensive auto-load first.
    try:
        images, image_workspace = await resolve_public_images(images, app_state(request).settings)
    except MLXBarError as exc:
        raise HTTPException(exc.status, detail=exc.as_dict()["error"])
    image_root = image_workspace.path if image_workspace else None
    try:
        loaded = await _ensure_requested_model(request, requested_model)
    except MLXBarError as exc:
        if image_workspace:
            image_workspace.cleanup()
        raise HTTPException(exc.status, detail=exc.as_dict()["error"])
    response_model = loaded.get("name") or loaded.get("id") or requested_model
    try:
        capacity_check = getattr(app_state(request).workers, "raise_if_queue_full", None)
        if capacity_check:
            capacity_check()
    except MLXBarError as exc:
        if image_workspace:
            image_workspace.cleanup()
        raise HTTPException(exc.status, detail=exc.as_dict()["error"])
    if body.get("stream", False):
        async def stream():
            usage = _usage(normalized_messages, "")
            completion_text = ""
            created = int(time.time())
            completed = False
            failed = False
            generation = None

            def mark_first_token() -> None:
                if request.state.api_log.get("first_token_ms") is not None:
                    return
                origin = getattr(request.state, "api_started_monotonic", None)
                if origin is not None:
                    request.state.api_log["first_token_ms"] = round(
                        (time.monotonic() - origin) * 1000)
            try:
                initial = {"id": request_id, "object": "chat.completion.chunk", "created": created,
                           "model": response_model, "choices": [{"index": 0,
                           "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
                yield "data: " + json.dumps(initial, ensure_ascii=False) + "\n\n"
                generation = app_state(request).workers.generate(
                    normalized_messages, images, options, request_id, image_root=image_root)
                async for event in generation:
                    if event.get("type") == "delta":
                        mark_first_token()
                        completion_text += event["text"]
                        usage = _usage(normalized_messages, completion_text)
                        delta = {"content": event["text"]}
                        chunk = {"id": request_id, "object": "chat.completion.chunk", "created": created,
                                 "model": response_model, "choices": [{"index": 0,
                                 "delta": delta, "finish_reason": None}]}
                        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                    elif event.get("type") == "reasoning_delta":
                        mark_first_token()
                        chunk = {"id": request_id, "object": "chat.completion.chunk", "created": created,
                                 "model": response_model, "choices": [{"index": 0,
                                 "delta": {"reasoning_content": event.get("text", "")},
                                 "finish_reason": None}]}
                        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                    elif event.get("type") == "tool_calls":
                        mark_first_token()
                        for chunk in _tool_call_stream_chunks(request_id, response_model, event.get("calls") or [], created):
                            yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                    elif event.get("type") == "tool_call_delta":
                        mark_first_token()
                        chunk = {"id": request_id, "object": "chat.completion.chunk", "created": created,
                                 "model": response_model, "choices": [{"index": 0,
                                 "delta": {"role": "assistant", "tool_calls": event.get("calls") or []},
                                 "finish_reason": None}]}
                        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                    elif event.get("type") == "completed":
                        if completed:
                            continue
                        finish_reason = event.get("finish_reason", "stop")
                        if finish_reason not in {"stop", "length", "tool_calls", "content_filter", "function_call"}:
                            finish_reason = "stop"
                        chunk = {"id": request_id, "object": "chat.completion.chunk", "created": created,
                                 "model": response_model, "choices": [{"index": 0, "delta": {},
                                 "finish_reason": finish_reason}]}
                        yield "data: " + json.dumps(chunk) + "\n\n"
                        completed = True
                    elif event.get("type") == "usage":
                        usage = _usage_from_event(event, usage)
                        request.state.api_log["prompt_tokens"] = usage["prompt_tokens"]
                    elif event.get("type") == "metrics":
                        for key in ("prompt_tokens", "cached_tokens", "prompt_tps", "generation_tps",
                                    "cache_tier", "tool_support", "cold_reason",
                                    "shared_prefix_tokens", "held_prefix_tokens"):
                            if event.get(key) is not None:
                                request.state.api_log[key] = event[key]
                    elif event.get("type") == "tool_support":
                        request.state.api_log["tool_support"] = event.get("state")
                    elif event.get("type") in {"phase", "heartbeat", "queue", "progress"}:
                        # SSE comments are ignored by OpenAI clients, but keep the
                        # connection alive during long tokenization/prefill.
                        yield ": mlxbar keep-alive\n\n"
                    elif event.get("type") == "error":
                        failed = True
                        request.state.api_log["error_code"] = event.get("code", "GENERATION_FAILED")
                        yield "data: " + json.dumps({"error": {"code": event.get("code", "GENERATION_FAILED"),
                              "message": event.get("message", "生成に失敗しました"),
                              "retryable": event.get("retryable", False)}}, ensure_ascii=False) + "\n\n"
                        break
            except asyncio.CancelledError:
                request.state.api_log["error_code"] = "CLIENT_DISCONNECTED"
                raise
            except MLXBarError as exc:
                failed = True
                request.state.api_log["error_code"] = exc.code
                yield "data: " + json.dumps(exc.as_dict(), ensure_ascii=False) + "\n\n"
            except Exception as exc:
                failed = True
                request.state.api_log["error_code"] = "INTERNAL_ERROR"
                yield "data: " + json.dumps({"error": {"code": "INTERNAL_ERROR", "message": str(exc),
                                                         "retryable": False}}, ensure_ascii=False) + "\n\n"
            finally:
                if generation is not None:
                    await generation.aclose()
                if image_workspace:
                    image_workspace.cleanup()
            if not failed and not completed:
                chunk = {"id": request_id, "object": "chat.completion.chunk", "created": created,
                         "model": response_model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield "data: " + json.dumps(chunk) + "\n\n"
            if not failed and (body.get("stream_options") or {}).get("include_usage"):
                yield "data: " + json.dumps({"id": request_id, "object": "chat.completion.chunk",
                    "created": created, "model": response_model, "choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                          "X-Accel-Buffering": "no"})
    text = ""
    tool_calls: list[dict] = []
    finish_reason = "stop"
    usage = None
    generation = app_state(request).workers.generate(
        normalized_messages, images, options, request_id, image_root=image_root)
    try:
        async for event in generation:
            if event.get("type") == "delta":
                if request.state.api_log.get("first_token_ms") is None:
                    origin = getattr(request.state, "api_started_monotonic", None)
                    if origin is not None:
                        request.state.api_log["first_token_ms"] = round(
                            (time.monotonic() - origin) * 1000)
                text += event["text"]
            elif event.get("type") == "tool_calls":
                tool_calls = _normalize_output_tool_calls(event.get("calls") or [])
                finish_reason = "tool_calls" if tool_calls else finish_reason
            elif event.get("type") == "tool_call_delta":
                tool_calls = _merge_tool_call_deltas(tool_calls, event.get("calls") or [])
            elif event.get("type") == "completed":
                finish_reason = event.get("finish_reason") or finish_reason
            elif event.get("type") == "usage":
                usage = _usage_from_event(event)
                request.state.api_log["prompt_tokens"] = usage["prompt_tokens"]
            elif event.get("type") == "metrics":
                for key in ("prompt_tokens", "cached_tokens", "prompt_tps", "generation_tps",
                            "cache_tier", "tool_support", "cold_reason",
                            "shared_prefix_tokens", "held_prefix_tokens"):
                    if event.get(key) is not None:
                        request.state.api_log[key] = event[key]
            elif event.get("type") == "tool_support":
                request.state.api_log["tool_support"] = event.get("state")
            elif event.get("type") == "error":
                raise MLXBarError(event.get("code", "GENERATION_FAILED"),
                                  event.get("message", "生成に失敗しました"), 502,
                                  event.get("retryable", False))
    except MLXBarError as exc:
        raise HTTPException(exc.status, detail=exc.as_dict()["error"])
    finally:
        await generation.aclose()
        if image_workspace:
            image_workspace.cleanup()
    message = {"role": "assistant", "content": text or None if tool_calls else text}
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {"id": request_id, "object": "chat.completion", "created": int(time.time()),
            "model": response_model, "choices": [{"index": 0,
            "message": message, "finish_reason": finish_reason}],
            "usage": usage or _usage(normalized_messages, text)}


async def _ensure_requested_model(request: Request, requested: str) -> dict:
    state = app_state(request)
    loaded = state.workers.loaded
    if loaded and _model_matches(loaded, requested, state):
        return loaded
    settings_models = state.settings.data.get("models", {})
    if not settings_models.get("autoLoadOnAPIRequest", True):
        raise MLXBarError("MODEL_NOT_LOADED", "モデルがロードされていません。MLXBarでモデルをロードしてください", 409)
    if state.database.metadata_value("api_autoload_suspended") == "1":
        raise MLXBarError("MODEL_NOT_LOADED", "モデルは手動で停止されています。MLXBarでモデルをロードしてください", 409)
    lock = getattr(state, "model_autoload_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        state.model_autoload_lock = lock
    async with lock:
        loaded = state.workers.loaded
        if loaded and _model_matches(loaded, requested, state):
            return loaded
        if loaded and (getattr(state.workers, "active_requests", {})
                       or getattr(state.workers, "queued_requests", {})):
            # Queued requests resolve against whatever model is loaded when
            # their turn comes, so switching under them is as disruptive as
            # switching under a running one.
            raise MLXBarError("ENGINE_BUSY", "別のモデルが応答中のため、モデルを切り替えられません", 429, True)
        model = _find_model(state, requested)
        if not model:
            raise MLXBarError("MODEL_NOT_FOUND", f"モデル「{requested}」が見つかりません。モデル一覧を再スキャンしてください", 404)
        result = await state.workers.load(model)
        state.database.set_metadata_value("last_loaded_model_id", model["id"])
        state.database.set_metadata_value("api_autoload_suspended", "0")
        _record_cache_capability(state, result)
        return result


def _record_cache_capability(state, loaded: dict) -> None:
    """Mirror of the management route's record, for models loaded by an API call."""
    from .management import _record_cache_capability as record
    record(state, loaded)


def _model_matches(model: dict, requested: str, state) -> bool:
    value = requested.strip().casefold()
    candidates = {str(model.get(key, "")).casefold() for key in ("id", "name", "provider_key")}
    if value in candidates:
        return True
    if value in {"loaded", "current_model", "local", "x", "openai/x"}:
        return True
    if value.startswith("openai/") and value[7:] in candidates:
        return True
    return False


def _find_model(state, requested: str) -> dict | None:
    models = state.database.list_models()
    value = requested.strip().casefold()
    aliases = {"loaded", "current_model", "local", "x", "openai/x"}
    if value in aliases:
        last_id = state.database.metadata_value("last_loaded_model_id")
        if last_id:
            for model in models:
                if model.get("id") == last_id:
                    return model
        return models[0] if len(models) == 1 else None
    if value.startswith("openai/"):
        value = value[7:]
    for model in models:
        if value in {str(model.get(key, "")).casefold() for key in ("id", "name", "provider_key")}:
            return model
    return None


def _model_descriptor(state, model: dict, loaded: dict | None) -> dict:
    is_loaded = bool(loaded and model.get("id") == loaded.get("id"))
    capabilities = (loaded.get("capabilities") if is_loaded and loaded else {}) or {}
    maximum = None
    if is_loaded and hasattr(state.workers, "effective_max_tokens"):
        maximum = state.workers.effective_max_tokens()
    descriptor = {"id": model.get("name") or model.get("id"), "object": "model", "created": 0,
                  "owned_by": "mlxbar", "loaded": is_loaded, "max_tokens": maximum,
                  "context_window": capabilities.get("modelMaxTokens")}
    if is_loaded:
        # What a client needs in order to keep its own prompt inside the range
        # where reuse actually happens. Advertising it here is cheaper for
        # everyone than having the client discover the cliff by falling off it.
        cache = capabilities.get("promptCache") or {}
        descriptor["prefix_reuse"] = capabilities.get("rollbackCapability")
        affordable = (cache.get("checkpoint") or {}).get("affordableTokens")
        if isinstance(affordable, int) and affordable > 0:
            descriptor["recommended_max_prompt_tokens"] = affordable
    return descriptor


def _usage(messages: list[dict], text: str) -> dict:
    prompt_chars = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    prompt_tokens = max(1, (prompt_chars + 3) // 4)
    completion_tokens = (len(text) + 3) // 4
    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens}


def _usage_from_event(event: dict, fallback: dict | None = None) -> dict:
    prompt = int(event.get("prompt_tokens", (fallback or {}).get("prompt_tokens", 0)))
    completion = int(event.get("completion_tokens", (fallback or {}).get("completion_tokens", 0)))
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _normalize_input_tool_calls(calls: list) -> list[dict]:
    result = _normalize_output_tool_calls(calls)
    if len(result) != len(calls):
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "tool_calls形式が不正です"})
    return result


def _normalize_extra_body(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(422, detail={"code": "INVALID_REQUEST",
            "message": "extra_bodyはオブジェクトで指定してください", "param": "extra_body"})
    kwargs = value.get("chat_template_kwargs", {})
    if not isinstance(kwargs, dict):
        raise HTTPException(422, detail={"code": "INVALID_REQUEST",
            "message": "chat_template_kwargsはオブジェクトで指定してください",
            "param": "extra_body.chat_template_kwargs"})
    reserved = set(kwargs) & RESERVED_CHAT_TEMPLATE_KWARGS
    if reserved:
        raise HTTPException(400, detail={"code": "UNSUPPORTED_PARAMETER",
            "parameters": [f"extra_body.chat_template_kwargs.{key}" for key in sorted(reserved)]})
    result = dict(kwargs)
    thinking = value.get("thinking")
    if thinking is not None:
        for key, normalized in _normalize_thinking(thinking).items():
            result.setdefault(key, normalized)
    reasoning_effort = value.get("reasoning_effort")
    if reasoning_effort is not None:
        _apply_reasoning_effort(result, reasoning_effort, "extra_body.reasoning_effort")
    return result


def _reject_unimplemented(body: dict) -> None:
    """Fail loudly on known OpenAI options MLXBar cannot honour.

    Ignoring an unknown vendor extension is right -- clients add those over
    time. Ignoring a *standard* option the caller is relying on is not: a
    client asking for JSON mode and quietly getting prose has no way to tell.
    """
    response_format = body.get("response_format")
    if response_format is not None:
        kind = response_format.get("type") if isinstance(response_format, dict) else None
        if kind != "text":
            raise HTTPException(400, detail={
                "code": "UNSUPPORTED_PARAMETER",
                "message": "response_formatはtextのみ対応しています。"
                           "構造化出力はプロンプトとtool callingで指定してください",
                "param": "response_format", "parameters": ["response_format"]})
    if body.get("logprobs"):
        raise HTTPException(400, detail={
            "code": "UNSUPPORTED_PARAMETER", "message": "logprobsには対応していません",
            "param": "logprobs", "parameters": ["logprobs"]})


def _json_size(value) -> int:
    """Return serialized character count without retaining private content."""
    if value is None:
        return 0
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


def _reasoning_mode(kwargs: dict) -> str:
    if kwargs.get("enable_thinking") is False:
        return "disabled"
    effort = kwargs.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        normalized = effort.strip().casefold()
        return normalized if normalized in {"none", "minimal", "low", "medium", "high", "xhigh"} else "custom"
    if kwargs.get("enable_thinking") is True:
        return "enabled"
    return "model_default"


def _normalize_chat_template_kwargs(body: dict) -> dict:
    kwargs = _normalize_extra_body(body.get("extra_body"))
    thinking = body.get("thinking")
    if thinking is not None:
        for key, value in _normalize_thinking(thinking).items():
            kwargs.setdefault(key, value)
    reasoning_effort = body.get("reasoning_effort")
    if reasoning_effort is not None:
        _apply_reasoning_effort(kwargs, reasoning_effort, "reasoning_effort")
    return kwargs


def _apply_reasoning_effort(kwargs: dict, value, parameter: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(422, detail={"code": "INVALID_REQUEST",
            "message": "reasoning_effortは空でない文字列で指定してください", "param": parameter})
    effort = value.strip().casefold()
    kwargs.setdefault("reasoning_effort", effort)
    if effort == "none":
        kwargs.setdefault("enable_thinking", False)


def _normalize_thinking(value) -> dict:
    if isinstance(value, bool):
        return {"enable_thinking": value}
    if not isinstance(value, dict):
        raise HTTPException(422, detail={"code": "INVALID_REQUEST",
            "message": "thinkingはブール値またはオブジェクトで指定してください", "param": "thinking"})
    result = {}
    thinking_type = value.get("type")
    if thinking_type is not None:
        if not isinstance(thinking_type, str) or thinking_type not in {"enabled", "disabled", "adaptive"}:
            raise HTTPException(422, detail={"code": "INVALID_REQUEST",
                "message": "thinking.typeはenabled、disabled、adaptiveのいずれかです",
                "param": "thinking.type"})
        result["enable_thinking"] = thinking_type != "disabled"
    budget = value.get("budget_tokens")
    if budget is not None:
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
            raise HTTPException(422, detail={"code": "INVALID_REQUEST",
                "message": "thinking.budget_tokensは正の整数で指定してください",
                "param": "thinking.budget_tokens"})
        result.setdefault("enable_thinking", True)
        result["thinking_budget"] = budget
    clear_thinking = value.get("clear_thinking")
    if clear_thinking is not None:
        if not isinstance(clear_thinking, bool):
            raise HTTPException(422, detail={"code": "INVALID_REQUEST",
                "message": "thinking.clear_thinkingはブール値で指定してください",
                "param": "thinking.clear_thinking"})
        result["preserve_thinking"] = not clear_thinking
    effort = value.get("effort")
    if effort is not None:
        _apply_reasoning_effort(result, effort, "thinking.effort")
    return result


def _normalize_tools(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 128:
        raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "toolsは最大128件の配列です"})
    result = []
    for tool in value:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "function tool形式が不正です"})
        result.append(tool)
    return result


def _normalize_tool_choice(value, tools: list):
    if value is None:
        return "auto" if tools else None
    if isinstance(value, str) and value in {"none", "auto", "required"}:
        return value
    if isinstance(value, dict) and value.get("type") == "function":
        function = value.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            return value
    raise HTTPException(422, detail={"code": "INVALID_REQUEST", "message": "tool_choice形式が不正です"})


def _normalize_output_tool_calls(calls: list) -> list[dict]:
    result = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function", call)
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            continue
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        result.append({"id": call.get("id") or "call_" + uuid.uuid4().hex, "type": "function",
                       "function": {"name": function["name"], "arguments": arguments}})
    return result


def _merge_tool_call_deltas(current: list[dict], deltas: list[dict]) -> list[dict]:
    for position, delta in enumerate(deltas):
        index = int(delta.get("index", position))
        while len(current) <= index:
            current.append({"id": "call_" + uuid.uuid4().hex, "type": "function",
                            "function": {"name": "", "arguments": ""}})
        target = current[index]
        if delta.get("id"): target["id"] = delta["id"]
        function = delta.get("function") or {}
        if function.get("name"): target["function"]["name"] += function["name"]
        if function.get("arguments"): target["function"]["arguments"] += function["arguments"]
    return current


def _tool_call_stream_chunks(request_id: str, model: str, calls: list[dict], created: int | None = None):
    normalized = _normalize_output_tool_calls(calls)
    created = created or int(time.time())
    for index, call in enumerate(normalized):
        first = {"index": index, "id": call["id"], "type": "function",
                 "function": {"name": call["function"]["name"], "arguments": ""}}
        yield {"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model,
               "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [first]}, "finish_reason": None}]}
        arguments = call["function"]["arguments"]
        for offset in range(0, len(arguments), 256):
            delta = {"index": index, "function": {"arguments": arguments[offset:offset + 256]}}
            yield {"id": request_id, "object": "chat.completion.chunk", "created": created, "model": model,
                   "choices": [{"index": 0, "delta": {"tool_calls": [delta]}, "finish_reason": None}]}
