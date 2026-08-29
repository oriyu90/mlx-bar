"""Anthropic Messages API compatibility, isolated under ``/anthropic``.

Design: this is an *input/output adapter only*. Every request is translated into
the exact internal shape the existing worker pool already accepts
(``messages`` + generation ``options``), run through the same
``generate_for_model`` / queue / cancel machinery the OpenAI surface uses, and
the worker's events are translated back into Anthropic's streaming shape by
``anthropic_stream``. The OpenAI surface is not touched.

Deliberately not implemented in v1 (returned as an explicit
``invalid_request_error`` rather than silently ignored): Anthropic server-side
tools (web search etc.), extended thinking with signed blocks, PDF/`document`
content blocks, and Anthropic-side MCP execution. ``cache_control`` hints are
accepted and ignored; MLXBar never reports Anthropic cache-usage figures.
"""

from __future__ import annotations

import json
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import MLXBarError
from .images import resolve_public_images
from .anthropic_stream import AnthropicMessageBuilder, sse, _anthropic_error_type
from .openai_compat import (
    _ensure_requested_model, _find_model, _is_generatable, app_state,
)


ANTHROPIC_CONTENT_TYPES = {"text", "image", "tool_use", "tool_result"}
MAX_TOOLS = 128


def _request_id() -> str:
    return "req_" + secrets.token_hex(16)


def _error(status: int, kind: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"type": kind, "message": message})


def _bad_request(message: str) -> HTTPException:
    return _error(400, "invalid_request_error", message)


# --------------------------------------------------------------------------
# request translation
# --------------------------------------------------------------------------

def _text_of(blocks) -> str:
    """Flatten an Anthropic content value (str or block list) to plain text."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _system_text(system) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        text = _text_of(system)
        return text or None
    raise _bad_request("system は文字列またはテキストブロックの配列で指定してください")


def _normalize_tools(tools) -> list[dict]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise _bad_request("tools は配列で指定してください")
    if len(tools) > MAX_TOOLS:
        raise _bad_request(f"tools は最大{MAX_TOOLS}件です")
    result = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise _bad_request("tool は name と input_schema を持つオブジェクトで指定してください")
        kind = tool.get("type")
        if kind not in (None, "custom"):
            raise _bad_request(
                f"サーバーサイドツール（type={kind}）には対応していません。"
                "クライアント側で実行する関数ツールのみ利用できます")
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            raise _bad_request(f"tool {tool['name']} の input_schema はオブジェクトで指定してください")
        result.append({"type": "function", "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": schema,
        }})
    return result


def _normalize_tool_choice(choice, tools: list) -> object:
    if choice is None:
        return "auto" if tools else None
    if not isinstance(choice, dict):
        raise _bad_request("tool_choice はオブジェクトで指定してください")
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = choice.get("name")
        if not isinstance(name, str) or not name:
            raise _bad_request("tool_choice.type=tool には name が必要です")
        return {"type": "function", "function": {"name": name}}
    raise _bad_request("tool_choice.type は auto / any / tool / none のいずれかです")


def _translate_messages(body: dict) -> tuple[list[dict], list[str]]:
    """Anthropic messages -> internal messages + flat image reference list."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _bad_request("messages は1件以上の配列で指定してください")

    internal: list[dict] = []
    images: list[str] = []
    system = _system_text(body.get("system"))
    if system is not None:
        internal.append({"role": "system", "content": system})

    for message in messages:
        if not isinstance(message, dict):
            raise _bad_request("message はオブジェクトで指定してください")
        role = message.get("role")
        if role not in {"user", "assistant"}:
            raise _bad_request("message.role は user または assistant です")
        content = message.get("content")
        if isinstance(content, str):
            internal.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise _bad_request("content は文字列またはブロックの配列で指定してください")

        text_parts: list[dict] = []
        tool_calls: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                raise _bad_request("content ブロックはオブジェクトで指定してください")
            btype = block.get("type")
            if btype not in ANTHROPIC_CONTENT_TYPES:
                raise _bad_request(
                    f"未対応の content ブロック type={btype} です。"
                    "対応しているのは text / image / tool_use / tool_result です")
            if btype == "text":
                text_parts.append({"type": "text", "text": str(block.get("text", ""))})
            elif btype == "image":
                ref = _image_ref(block.get("source"))
                images.append(ref)
                text_parts.append({"type": "image_url", "image_url": {"url": ref}})
            elif btype == "tool_use":
                if role != "assistant":
                    raise _bad_request("tool_use ブロックは assistant メッセージにのみ含められます")
                tool_calls.append({
                    "id": block.get("id") or ("toolu_" + secrets.token_hex(8)),
                    "type": "function",
                    "function": {"name": block.get("name", ""),
                                 "arguments": json.dumps(block.get("input", {}),
                                                         ensure_ascii=False)},
                })
            elif btype == "tool_result":
                if role != "user":
                    raise _bad_request("tool_result ブロックは user メッセージにのみ含められます")
                # A tool result is its own turn in the internal transcript.
                result_text = _text_of(block.get("content"))
                if block.get("is_error"):
                    result_text = f"[error] {result_text}"
                internal.append({"role": "tool",
                                 "tool_call_id": block.get("tool_use_id", ""),
                                 "content": result_text})

        if tool_calls:
            entry: dict = {"role": "assistant", "tool_calls": tool_calls}
            plain = _parts_to_content(text_parts)
            entry["content"] = plain if plain not in ("", []) else None
            internal.append(entry)
        elif text_parts:
            internal.append({"role": role, "content": _parts_to_content(text_parts)})

    if not any(m["role"] in {"user", "assistant"} for m in internal):
        raise _bad_request("user または assistant のメッセージが必要です")
    return internal, images


def _parts_to_content(parts: list[dict]):
    if not parts:
        return ""
    if all(part.get("type") == "text" for part in parts):
        return "".join(part.get("text", "") for part in parts)
    return parts


def _image_ref(source) -> str:
    if not isinstance(source, dict):
        raise _bad_request("image.source はオブジェクトで指定してください")
    stype = source.get("type")
    if stype == "base64":
        media_type = source.get("media_type") or "image/png"
        data = source.get("data")
        if not isinstance(data, str) or not data:
            raise _bad_request("image.source.data が不正です")
        return f"data:{media_type};base64,{data}"
    if stype == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise _bad_request("image.source.url が不正です")
        return url
    raise _bad_request("image.source.type は base64 または url です")


def _generation_options(body: dict, tools: list, tool_choice) -> dict:
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise _bad_request("max_tokens は1以上の整数で指定してください")
    if body.get("thinking") is not None:
        raise _bad_request(
            "extended thinking（thinking パラメータ）には現時点で対応していません")
    raw_choice = body.get("tool_choice")
    disable_parallel = bool(raw_choice.get("disable_parallel_tool_use", False)
                            if isinstance(raw_choice, dict) else False)
    options: dict = {"max_tokens": max_tokens,
                     "tools": [] if tool_choice == "none" else tools,
                     "tool_choice": tool_choice,
                     "parallel_tool_calls": not disable_parallel}
    if isinstance(tool_choice, dict):
        selected = tool_choice["function"]["name"]
        options["tools"] = [t for t in tools if t["function"]["name"] == selected] or tools
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            options[dst] = body[src]
    stops = body.get("stop_sequences")
    if stops is not None:
        if not isinstance(stops, list) or not all(isinstance(s, str) for s in stops):
            raise _bad_request("stop_sequences は文字列の配列で指定してください")
        options["stop"] = stops
    return options


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

async def _messages(request: Request):
    state = app_state(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise _bad_request("リクエスト本文は JSON オブジェクトで指定してください")
    requested_model = body.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise _bad_request("model を指定してください")
    stream = bool(body.get("stream", False))

    tools = _normalize_tools(body.get("tools"))
    tool_choice = _normalize_tool_choice(body.get("tool_choice"), tools)
    messages, image_refs = _translate_messages(body)
    options = _generation_options(body, tools, tool_choice)

    request.state.api_log = {
        "model": requested_model, "stream": stream,
        "message_count": len(body.get("messages") or []),
        "tool_count": len(tools), "max_tokens": options["max_tokens"],
    }

    try:
        images, workspace = await resolve_public_images(image_refs, state.settings)
    except MLXBarError as exc:
        raise HTTPException(exc.status, detail={
            "type": _anthropic_error_type(exc.code), "message": exc.message})
    image_root = workspace.path if workspace else None

    try:
        loaded = await _ensure_requested_model(request, requested_model)
    except MLXBarError as exc:
        if workspace:
            workspace.cleanup()
        raise HTTPException(exc.status, detail={
            "type": _anthropic_error_type(exc.code), "message": exc.message})

    capacity_check = getattr(state.workers, "raise_if_queue_full", None)
    if capacity_check:
        try:
            capacity_check()
        except MLXBarError as exc:
            if workspace:
                workspace.cleanup()
            raise HTTPException(exc.status, detail={
                "type": _anthropic_error_type(exc.code), "message": exc.message})

    response_model = loaded.get("name") or loaded.get("id") or requested_model
    request_id = "chatcmpl-" + secrets.token_hex(12)
    builder = AnthropicMessageBuilder(response_model, _estimate_prompt_tokens(messages))
    generate_for_model = getattr(state.workers, "generate_for_model", None)

    def _generation():
        if generate_for_model:
            return generate_for_model(str(loaded.get("id", "")), messages, images, options,
                                      request_id, image_root=image_root)
        return state.workers.generate(messages, images, options, request_id, image_root=image_root)

    if stream:
        async def event_stream():
            generation = _generation()
            try:
                for start in builder.stream_start():
                    yield sse(start)
                async for event in generation:
                    for out in builder.stream_events(event):
                        yield sse(out)
                        if out.get("__error__"):
                            return
            except MLXBarError as exc:
                yield sse({"__error__": True, "type": "error",
                           "error": {"type": _anthropic_error_type(exc.code),
                                     "message": exc.message}})
            except Exception as exc:  # noqa: BLE001
                request.state.api_log["error_code"] = "INTERNAL_ERROR"
                yield sse({"__error__": True, "type": "error",
                           "error": {"type": "api_error", "message": str(exc)}})
            finally:
                if generation is not None:
                    await generation.aclose()
                if workspace:
                    workspace.cleanup()
        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                          "X-Accel-Buffering": "no",
                                          "request-id": _request_id()})

    generation = _generation()
    try:
        async for event in generation:
            if event.get("type") == "error":
                raise MLXBarError(event.get("code", "GENERATION_FAILED"),
                                  event.get("message", "生成に失敗しました"), 502,
                                  event.get("retryable", False))
            builder.handle(event)
    except MLXBarError as exc:
        raise HTTPException(exc.status if exc.status >= 400 else 502, detail={
            "type": _anthropic_error_type(exc.code), "message": exc.message})
    finally:
        await generation.aclose()
        if workspace:
            workspace.cleanup()
    return JSONResponse(builder.final_message(), headers={"request-id": _request_id()})


async def _count_tokens(request: Request):
    state = app_state(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise _bad_request("リクエスト本文は JSON オブジェクトで指定してください")
    requested_model = body.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise _bad_request("model を指定してください")
    tools = _normalize_tools(body.get("tools"))
    tool_choice = _normalize_tool_choice(body.get("tool_choice"), tools)
    messages, _ = _translate_messages(body)
    options = {"tools": tools, "tool_choice": tool_choice}

    request.state.api_log = {"model": requested_model, "path": "/anthropic/v1/messages/count_tokens"}
    try:
        loaded = await _ensure_requested_model(request, requested_model)
        counter = getattr(state.workers, "count_tokens", None)
        if not callable(counter):
            raise MLXBarError("COUNT_TOKENS_UNAVAILABLE",
                              "このランタイムはトークン数の計測に対応していません", 503, False)
        result = await counter(str(loaded.get("id", "")), messages, options)
    except MLXBarError as exc:
        raise HTTPException(exc.status, detail={
            "type": _anthropic_error_type(exc.code), "message": exc.message})
    return JSONResponse({"input_tokens": int(result.get("input_tokens", 0))},
                        headers={"request-id": _request_id()})


async def _models(request: Request):
    state = app_state(request)
    data = []
    for model in state.database.list_models():
        if not _is_generatable(model):
            continue
        data.append({"type": "model",
                     "id": model.get("name") or model.get("id"),
                     "display_name": model.get("name") or model.get("id"),
                     "created_at": "1970-01-01T00:00:00Z"})
    return JSONResponse({"data": data, "has_more": False,
                         "first_id": data[0]["id"] if data else None,
                         "last_id": data[-1]["id"] if data else None},
                        headers={"request-id": _request_id()})


async def _model(request: Request, model_id: str):
    state = app_state(request)
    model = _find_model(state, model_id)
    if not model or not _is_generatable(model):
        raise _error(404, "not_found_error", f"モデル「{model_id}」が見つかりません")
    return JSONResponse({"type": "model",
                         "id": model.get("name") or model.get("id"),
                         "display_name": model.get("name") or model.get("id"),
                         "created_at": "1970-01-01T00:00:00Z"},
                        headers={"request-id": _request_id()})


def _estimate_prompt_tokens(messages: list[dict]) -> int:
    chars = len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    return max(1, (chars + 3) // 4)


# --------------------------------------------------------------------------
# app factory
# --------------------------------------------------------------------------

def make_anthropic_app(state) -> FastAPI:
    app = FastAPI(title="MLXBar Anthropic API", docs_url=None, redoc_url=None)
    app.state.mlxbar = state

    app.add_api_route("/v1/messages", _messages, methods=["POST"])
    app.add_api_route("/v1/messages/count_tokens", _count_tokens, methods=["POST"])
    app.add_api_route("/v1/models", _models, methods=["GET"])
    app.add_api_route("/v1/models/{model_id:path}", _model, methods=["GET"])

    def envelope(status: int, kind: str, message: str) -> JSONResponse:
        return JSONResponse(status_code=status, headers={"request-id": _request_id()}, content={
            "type": "error", "error": {"type": kind, "message": message},
            "request_id": "req_" + secrets.token_hex(12)})

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        api_log = getattr(request.state, "api_log", None)
        if isinstance(api_log, dict):
            api_log["error_code"] = f"HTTP_{exc.status_code}"
        return envelope(exc.status_code, detail.get("type", "invalid_request_error"),
                        detail.get("message") or "リクエストを処理できませんでした")

    @app.exception_handler(StarletteHTTPException)
    async def _starlette_http(request: Request, exc: StarletteHTTPException):
        if isinstance(exc, HTTPException):
            return await _http(request, exc)
        kind = "not_found_error" if exc.status_code == 404 else "invalid_request_error"
        return envelope(exc.status_code, kind, str(exc.detail) or "リクエストを処理できませんでした")

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        return envelope(400, "invalid_request_error",
                        f"リクエスト本文が不正です: {first.get('msg', 'invalid body')}")

    @app.exception_handler(Exception)
    async def _internal(request: Request, exc: Exception):
        import logging
        logging.getLogger(__name__).error("Anthropic request failed: %s %s",
                                          request.method, request.url.path, exc_info=exc)
        return envelope(500, "api_error", "内部エラーが発生しました")

    return app
