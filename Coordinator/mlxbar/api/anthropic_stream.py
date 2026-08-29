"""Translate MLXBar worker events into the Anthropic Messages shape.

The worker event stream is exactly the one `openai_compat` consumes; only the
*output* format differs. This module is that output layer and nothing else -- it
never talks to a worker or a model.

Anthropic streaming reference:
- message_start -> content_block_start -> content_block_delta* -> content_block_stop
  (repeated per block) -> message_delta -> message_stop
- tool calls stream as a `tool_use` block whose arguments arrive as
  `input_json_delta` (`partial_json`) fragments
- keep-alive is `ping`; there is no `[DONE]`
- a mid-stream failure is a single `event: error`, with no message_stop after it
"""

from __future__ import annotations

import json
import secrets


_FINISH_TO_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "cancelled": "end_turn",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}


def new_message_id() -> str:
    return "msg_" + secrets.token_hex(16)


def new_tool_use_id() -> str:
    return "toolu_" + secrets.token_hex(16)


class AnthropicMessageBuilder:
    """Accumulates worker events into Anthropic content blocks + stop reason.

    One instance per request. `stream_events()` yields Anthropic streaming
    events (dicts) for the SSE route; `final_message()` returns the single
    non-streaming object. Both are driven by the same `handle()` so the two
    routes cannot drift.
    """

    def __init__(self, model: str, input_tokens: int, *, message_id: str | None = None):
        self.model = model
        self.message_id = message_id or new_message_id()
        self.input_tokens = max(0, int(input_tokens or 0))
        self.content: list[dict] = []
        self.stop_reason: str | None = None
        self.stop_sequence: str | None = None
        self._output_chars = 0
        self._reported_output_tokens: int | None = None
        # streaming block bookkeeping
        self._index = -1
        self._open_kind: str | None = None
        self._tool_args_by_index: dict[int, str] = {}
        self._tool_id_by_index: dict[int, str] = {}
        self._failed = False

    # --- estimation ------------------------------------------------------

    @property
    def output_tokens(self) -> int:
        if self._reported_output_tokens is not None:
            return self._reported_output_tokens
        return max(1, (self._output_chars + 3) // 4) if self._output_chars else 0

    def _note_text(self, text: str) -> None:
        self._output_chars += len(text or "")

    # --- non-streaming assembly ---------------------------------------------

    def _append_text(self, text: str) -> None:
        if self.content and self.content[-1]["type"] == "text":
            self.content[-1]["text"] += text
        else:
            self.content.append({"type": "text", "text": text})

    def _append_tool_use(self, call: dict) -> None:
        function = call.get("function", call)
        name = function.get("name") or ""
        raw = function.get("arguments", "{}")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except (TypeError, ValueError):
            parsed = {}
        self.content.append({
            "type": "tool_use",
            "id": call.get("id") or new_tool_use_id(),
            "name": name,
            "input": parsed if isinstance(parsed, dict) else {},
        })

    def handle(self, event: dict) -> None:
        """Fold one worker event into the accumulated message (no output)."""
        kind = event.get("type")
        if kind == "delta":
            text = str(event.get("text", ""))
            if text:
                self._note_text(text)
                self._append_text(text)
        elif kind == "tool_calls":
            for call in event.get("calls") or []:
                self._append_tool_use(call)
            self.stop_reason = self.stop_reason or "tool_use"
        elif kind == "tool_call_delta":
            for delta in event.get("calls") or []:
                self._merge_tool_call_delta(delta)
        elif kind == "usage":
            prompt = event.get("prompt_tokens")
            if isinstance(prompt, int) and prompt > 0:
                self.input_tokens = prompt
            completion = event.get("completion_tokens")
            if isinstance(completion, int) and completion >= 0:
                self._reported_output_tokens = completion
        elif kind == "metrics":
            completion = event.get("completion_tokens")
            if isinstance(completion, int) and completion >= 0:
                self._reported_output_tokens = completion
        elif kind == "completed":
            reason = event.get("finish_reason", "stop")
            sequence = event.get("stop_sequence")
            if sequence:
                self.stop_reason = "stop_sequence"
                self.stop_sequence = sequence
            elif self.stop_reason != "tool_use":
                self.stop_reason = _FINISH_TO_STOP_REASON.get(reason, "end_turn")

    def _merge_tool_call_delta(self, delta: dict) -> None:
        index = int(delta.get("index", len(self.content)))
        function = delta.get("function") or {}
        if delta.get("id"):
            self._tool_id_by_index[index] = delta["id"]
        # ensure a content block exists for this index
        while len(self.content) <= index:
            self.content.append({"type": "tool_use",
                                 "id": self._tool_id_by_index.get(index) or new_tool_use_id(),
                                 "name": "", "input": {}})
        block = self.content[index]
        if block["type"] != "tool_use":
            return
        if function.get("name"):
            block["name"] += function["name"]
        if function.get("arguments"):
            self._tool_args_by_index[index] = self._tool_args_by_index.get(index, "") + function["arguments"]
            try:
                block["input"] = json.loads(self._tool_args_by_index[index])
            except (TypeError, ValueError):
                pass
        self.stop_reason = self.stop_reason or "tool_use"

    def final_message(self) -> dict:
        return {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": self.content or [{"type": "text", "text": ""}],
            "stop_reason": self.stop_reason or "end_turn",
            "stop_sequence": self.stop_sequence,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
        }

    # --- streaming --------------------------------------------------------

    def stream_start(self) -> list[dict]:
        return [{"type": "message_start", "message": {
            "id": self.message_id, "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
        }}]

    def _close_open_block(self) -> list[dict]:
        if self._open_kind is None:
            return []
        self._open_kind = None
        return [{"type": "content_block_stop", "index": self._index}]

    def _open_text_block(self) -> list[dict]:
        out = self._close_open_block()
        self._index += 1
        self._open_kind = "text"
        out.append({"type": "content_block_start", "index": self._index,
                    "content_block": {"type": "text", "text": ""}})
        return out

    def _open_tool_block(self, tool_id: str, name: str) -> list[dict]:
        out = self._close_open_block()
        self._index += 1
        self._open_kind = "tool_use"
        out.append({"type": "content_block_start", "index": self._index,
                    "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}})
        return out

    def stream_events(self, event: dict) -> list[dict]:
        """Anthropic streaming events for one worker event."""
        kind = event.get("type")
        if kind in {"phase", "heartbeat", "queue", "progress"}:
            return [{"type": "ping"}]
        if kind == "reasoning_delta":
            # v1: internal reasoning is not surfaced as a (signed) thinking block.
            return []
        if kind == "delta":
            text = str(event.get("text", ""))
            if not text:
                return []
            self._note_text(text)
            out: list[dict] = []
            if self._open_kind != "text":
                out += self._open_text_block()
            out.append({"type": "content_block_delta", "index": self._index,
                        "delta": {"type": "text_delta", "text": text}})
            return out
        if kind == "tool_calls":
            out = []
            for call in event.get("calls") or []:
                function = call.get("function", call)
                tool_id = call.get("id") or new_tool_use_id()
                out += self._open_tool_block(tool_id, function.get("name") or "")
                raw = function.get("arguments", "")
                if isinstance(raw, str) and raw.strip():
                    out.append({"type": "content_block_delta", "index": self._index,
                                "delta": {"type": "input_json_delta", "partial_json": raw}})
                out += self._close_open_block()
            self.stop_reason = "tool_use"
            return out
        if kind == "tool_call_delta":
            out = []
            for delta in event.get("calls") or []:
                function = delta.get("function") or {}
                if self._open_kind != "tool_use" or delta.get("id"):
                    tool_id = delta.get("id") or new_tool_use_id()
                    out += self._open_tool_block(tool_id, function.get("name") or "")
                if function.get("arguments"):
                    out.append({"type": "content_block_delta", "index": self._index,
                                "delta": {"type": "input_json_delta",
                                          "partial_json": function["arguments"]}})
            self.stop_reason = "tool_use"
            return out
        if kind == "usage":
            self.handle(event)
            return []
        if kind == "metrics":
            self.handle(event)
            return []
        if kind == "completed":
            self.handle(event)
            out: list[dict] = []
            if self._index < 0:
                # Nothing was ever opened (empty reply). Anthropic still frames
                # an empty text block.
                out += self._open_text_block()
            out += self._close_open_block()
            out.append({"type": "message_delta",
                        "delta": {"stop_reason": self.stop_reason or "end_turn",
                                  "stop_sequence": self.stop_sequence},
                        "usage": {"output_tokens": self.output_tokens}})
            out.append({"type": "message_stop"})
            return out
        if kind == "error":
            self._failed = True
            return [{"__error__": True,
                     "type": "error",
                     "error": {"type": _anthropic_error_type(event.get("code")),
                               "message": event.get("message") or "生成に失敗しました"}}]
        return []


def _anthropic_error_type(code: str | None) -> str:
    mapping = {
        "AUTHENTICATION_FAILED": "authentication_error",
        "MODEL_NOT_FOUND": "not_found_error",
        "MODEL_NOT_LOADED": "invalid_request_error",
        "QUEUE_FULL": "overloaded_error",
        "QUEUE_TIMEOUT": "overloaded_error",
        "ENGINE_BUSY": "overloaded_error",
        "MEMORY_PRESSURE": "overloaded_error",
        "MEMORY_BUDGET_EXCEEDED": "overloaded_error",
        "INPUT_TOO_LARGE": "invalid_request_error",
        "INVALID_REQUEST": "invalid_request_error",
        "UNSUPPORTED_PARAMETER": "invalid_request_error",
        "COUNT_TOKENS_UNAVAILABLE": "invalid_request_error",
    }
    return mapping.get(code or "", "api_error")


def sse(event: dict) -> str:
    """Serialise one Anthropic streaming event as an SSE frame."""
    if event.get("__error__"):
        payload = {k: v for k, v in event.items() if k != "__error__"}
        return "event: error\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    return f"event: {event['type']}\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
