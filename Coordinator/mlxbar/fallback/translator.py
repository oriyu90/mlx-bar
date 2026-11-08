"""Internal request shape -> each external provider's request body.

Everything here is a pure function of its arguments: no I/O, no settings reads.
That is what lets the three protocols be tested side by side, and it keeps the
"what exactly do we send where" question answerable by reading one file.

Internal messages are the same shape the worker pool accepts (see
``api/openai_compat.py``): ``role`` (system/developer/user/assistant/tool),
``content`` (str or a list of ``{"type": "text"|"image_url"}`` parts),
``tool_calls`` on an assistant turn and ``tool_call_id`` on a tool turn. Images
arrive as files on disk (``ImageWorkspace``) and are inlined as base64 here --
never as a path, so nothing about this Mac's filesystem leaves the machine.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

#: Rough characters-per-token used only for the optional context ceiling.
CHARS_PER_TOKEN = 4
#: Tokens charged for one attached image when estimating a context size.
IMAGE_TOKEN_ESTIMATE = 1024
#: Cap on a single base64 image payload (the API already bounds the file size).
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def _guess_media_type(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(str(path))
    if media_type and media_type.startswith("image/"):
        return media_type
    return "image/png"


def _read_image(path: str) -> tuple[str, str] | None:
    """Return ``(media_type, base64)`` for an image file, or None if unreadable."""
    try:
        target = Path(path)
        if target.stat().st_size > MAX_IMAGE_BYTES:
            return None
        data = target.read_bytes()
    except (OSError, ValueError):
        return None
    return _guess_media_type(target), base64.b64encode(data).decode("ascii")


def _image_parts(paths: list[str]) -> list[tuple[str, str]]:
    result = []
    for path in paths:
        loaded = _read_image(path)
        if loaded is not None:
            result.append(loaded)
    return result


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text", "")) for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _content_has_images(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url" for part in content)


def _openai_parts(content: Any) -> list[dict[str, Any]] | None:
    """The content as ordered parts, or None when it is plain text.

    Review M-5: a caller can put text *and* images in one message (the Anthropic
    surface does exactly that). Flattening to text before attaching the images
    loses where they sat, so a message that already carries `image_url` parts
    keeps its own order and is never given the attached images again.
    """
    if not isinstance(content, list):
        return None
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": "text", "text": str(part.get("text", ""))})
        elif part.get("type") == "image_url" and isinstance(part.get("image_url"), dict):
            parts.append({"type": "image_url", "image_url": dict(part["image_url"])})
    return parts or None


def estimate_tokens(messages: list[dict], images: list[str]) -> int:
    """Cheap upper-ish estimate used for the optional context ceiling."""
    characters = 0
    for message in messages:
        characters += len(_text_of(message.get("content")))
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                characters += len(json.dumps(call.get("function", {}), ensure_ascii=False))
        characters += len(str(message.get("role") or ""))
    return characters // CHARS_PER_TOKEN + IMAGE_TOKEN_ESTIMATE * len(images)


def trim_to_context(messages: list[dict], images: list[str], limit: int) -> tuple[list[dict], int]:
    """Drop the oldest non-system turns until the estimate fits ``limit``.

    Returns ``(messages, dropped)``. System/developer turns are always kept --
    they carry the caller's instructions and the response_format/rag blocks
    injected earlier -- and the most recent turn is never dropped, so a limit
    that is already too small still sends something meaningful.
    """
    if not limit or limit <= 0:
        return messages, 0
    if estimate_tokens(messages, images) <= limit:
        return messages, 0
    kept = list(messages)
    dropped = 0
    index = 0
    while index < len(kept) - 1:
        if estimate_tokens(kept, images) <= limit:
            break
        if kept[index].get("role") in {"system", "developer"}:
            index += 1
            continue
        del kept[index]
        dropped += 1
    return kept, dropped


def _stop_value(options: dict) -> Any:
    stop = options.get("stop")
    if isinstance(stop, str) and stop:
        return stop
    if isinstance(stop, list):
        values = [item for item in stop if isinstance(item, str) and item]
        return values or None
    return None


def _max_tokens(options: dict, model: dict) -> int:
    requested = options.get("max_tokens")
    override = model.get("maxOutputTokens") or 0
    try:
        value = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        value = 0
    if override and value:
        return min(value, int(override))
    if override:
        return int(override)
    return value or 1024


def _sampling(options: dict, model: dict) -> dict[str, Any]:
    result: dict[str, Any] = {}
    temperature = model.get("temperature")
    if temperature is None:
        temperature = options.get("temperature")
    if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
        result["temperature"] = float(temperature)
    top_p = model.get("topP")
    if top_p is None:
        top_p = options.get("top_p")
    if isinstance(top_p, (int, float)) and not isinstance(top_p, bool) and top_p:
        result["top_p"] = float(top_p)
    return result


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------
def openai_chat_messages(messages: list[dict], images: list[str],
                         detail: str = "auto") -> list[dict]:
    """Internal messages -> Chat Completions ``messages``.

    Attached images are appended to the **last user turn** as ``image_url``
    parts, which is where every OpenAI-compatible server expects them; a
    request with images but no user turn gets one so the images are not lost.
    """
    result: list[dict] = []
    for message in messages:
        role = message.get("role") or "user"
        entry: dict[str, Any] = {"role": "system" if role == "developer" else role}
        content = message.get("content")
        if role == "assistant":
            if content is not None:
                entry["content"] = _text_of(content) if not isinstance(content, str) else content
            calls = message.get("tool_calls") or []
            if calls:
                entry["tool_calls"] = [dict(call) for call in calls if isinstance(call, dict)]
        elif role == "tool":
            entry["content"] = _text_of(content)
            if message.get("tool_call_id"):
                entry["tool_call_id"] = message["tool_call_id"]
        else:
            ordered = _openai_parts(content)
            entry["content"] = ordered if ordered is not None else (
                _text_of(content) if not isinstance(content, str) else content)
        if message.get("name") and role != "tool":
            entry["name"] = message["name"]
        result.append(entry)
    encoded = _image_parts(images)
    if encoded:
        target = None
        for entry in reversed(result):
            if entry.get("role") == "user":
                target = entry
                break
        if target is None:
            target = {"role": "user", "content": ""}
            result.append(target)
        parts: list[dict[str, Any]] = []
        text = target.get("content")
        if isinstance(text, str) and text:
            parts.append({"type": "text", "text": text})
        elif isinstance(text, list):
            # The message already carried ordered parts: keep them, and append
            # only the images it did not already include.
            parts.extend(text)
            if any(part.get("type") == "image_url" for part in parts):
                target["content"] = parts
                return result
        for media_type, payload in encoded:
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{payload}",
                "detail": detail if detail in {"auto", "low", "high"} else "auto",
            }})
        target["content"] = parts
    return result


def openai_chat_body(model: dict, messages: list[dict], images: list[str],
                     options: dict, stream: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model.get("model"),
        "messages": openai_chat_messages(messages, images, str(model.get("imageDetail") or "auto")),
        "max_tokens": _max_tokens(options, model),
        "stream": bool(stream),
    }
    body.update(_sampling(options, model))
    stop = _stop_value(options)
    if stop is not None:
        body["stop"] = stop
    tools = options.get("tools") or []
    if tools:
        body["tools"] = tools
        choice = options.get("tool_choice")
        if choice is not None:
            body["tool_choice"] = choice
        if options.get("parallel_tool_calls") is False:
            body["parallel_tool_calls"] = False
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------
def _responses_input(messages: list[dict]) -> list[dict]:
    items: list[dict] = []
    for message in messages:
        role = message.get("role") or "user"
        if role in {"system", "developer"}:
            # Responses takes the system prompt as `instructions`; a system
            # message left in `input` is rejected by strict servers.
            continue
        content = message.get("content")
        if role == "assistant":
            calls = message.get("tool_calls") or []
            text = _text_of(content)
            if text:
                items.append({"role": "assistant",
                              "content": [{"type": "output_text", "text": text}]})
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments") or "{}",
                })
        elif role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": message.get("tool_call_id") or "",
                          "output": _text_of(content)})
        else:
            items.append({"role": "user",
                          "content": [{"type": "input_text", "text": _text_of(content)}]})
    return items


def openai_responses_body(model: dict, messages: list[dict], images: list[str],
                          options: dict, stream: bool) -> dict[str, Any]:
    instructions = "\n\n".join(_text_of(message.get("content")) for message in messages
                               if message.get("role") in {"system", "developer"}).strip()
    items = _responses_input(messages)
    encoded = _image_parts(images)
    if encoded:
        target = None
        for item in reversed(items):
            if item.get("role") == "user":
                target = item
                break
        if target is None:
            target = {"role": "user", "content": []}
            items.append(target)
        for media_type, payload in encoded:
            target["content"].append({
                "type": "input_image",
                "image_url": f"data:{media_type};base64,{payload}",
                "detail": str(model.get("imageDetail") or "auto"),
            })
    body: dict[str, Any] = {
        "model": model.get("model"),
        "input": items,
        "max_output_tokens": _max_tokens(options, model),
        "stream": bool(stream),
    }
    if instructions:
        body["instructions"] = instructions
    body.update(_sampling(options, model))
    tools = options.get("tools") or []
    if tools:
        body["tools"] = [{"type": "function", "name": tool.get("function", {}).get("name", ""),
                          "description": tool.get("function", {}).get("description", ""),
                          "parameters": tool.get("function", {}).get("parameters", {})}
                         for tool in tools if isinstance(tool, dict)]
        choice = options.get("tool_choice")
        if choice == "required":
            body["tool_choice"] = "required"
        elif choice == "none":
            body["tool_choice"] = "none"
        elif isinstance(choice, dict):
            body["tool_choice"] = {"type": "function",
                                   "name": choice.get("function", {}).get("name", "")}
    return body


# ---------------------------------------------------------------------------
# Anthropic Messages
# ---------------------------------------------------------------------------
def _anthropic_content(message: dict) -> Any:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return _text_of(content)


def anthropic_body(model: dict, messages: list[dict], images: list[str],
                   options: dict, stream: bool) -> dict[str, Any]:
    system_parts: list[str] = []
    turns: list[dict] = []
    for message in messages:
        role = message.get("role") or "user"
        if role in {"system", "developer"}:
            text = _text_of(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            turns.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or "",
                "content": _text_of(message.get("content")),
            }]})
            continue
        if role == "assistant":
            blocks: list[dict] = []
            text = _text_of(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                try:
                    parsed = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    parsed = {}
                blocks.append({"type": "tool_use", "id": call.get("id") or "",
                               "name": function.get("name") or "",
                               "input": parsed if isinstance(parsed, dict) else {}})
            if blocks:
                turns.append({"role": "assistant", "content": blocks})
            continue
        turns.append({"role": "user", "content": _anthropic_content(message)})
    encoded = _image_parts(images)
    if encoded:
        target = None
        for turn in reversed(turns):
            if turn.get("role") == "user":
                target = turn
                break
        if target is None:
            target = {"role": "user", "content": []}
            turns.append(target)
        if isinstance(target.get("content"), str):
            text = target["content"]
            target["content"] = ([{"type": "text", "text": text}] if text else [])
        for media_type, payload in encoded:
            target["content"].append({"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": payload}})
    body: dict[str, Any] = {
        "model": model.get("model"),
        "messages": turns,
        "max_tokens": _max_tokens(options, model),
        "stream": bool(stream),
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    body.update(_sampling(options, model))
    stop = _stop_value(options)
    if stop is not None:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    tools = options.get("tools") or []
    if tools:
        body["tools"] = [{"name": tool.get("function", {}).get("name", ""),
                          "description": tool.get("function", {}).get("description", ""),
                          "input_schema": tool.get("function", {}).get("parameters", {})}
                         for tool in tools if isinstance(tool, dict)]
        choice = options.get("tool_choice")
        if choice == "required":
            body["tool_choice"] = {"type": "any"}
        elif choice == "none":
            body["tool_choice"] = {"type": "none"}
        elif isinstance(choice, dict):
            body["tool_choice"] = {"type": "tool",
                                   "name": choice.get("function", {}).get("name", "")}
        if options.get("parallel_tool_calls") is False:
            body["tool_choice"] = {**body.get("tool_choice", {"type": "auto"}),
                                   "disable_parallel_tool_use": True}
    return body


def build_body(protocol: str, model: dict, messages: list[dict], images: list[str],
               options: dict, stream: bool) -> dict[str, Any]:
    if protocol == "openai-responses":
        return openai_responses_body(model, messages, images, options, stream)
    if protocol == "anthropic-messages":
        return anthropic_body(model, messages, images, options, stream)
    return openai_chat_body(model, messages, images, options, stream)


def endpoint_path(protocol: str) -> str:
    if protocol == "openai-responses":
        return "/responses"
    if protocol == "anthropic-messages":
        return "/messages"
    return "/chat/completions"


def auth_headers(protocol: str, api_key: str | None,
                 extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    if api_key:
        if protocol == "anthropic-messages":
            headers["x-api-key"] = api_key
        else:
            headers["authorization"] = f"Bearer {api_key}"
    if protocol == "anthropic-messages":
        headers["anthropic-version"] = "2023-06-01"
    for key, value in (extra or {}).items():
        if isinstance(key, str) and isinstance(value, str) and key.strip():
            headers[key.strip().lower()] = value
    return headers
