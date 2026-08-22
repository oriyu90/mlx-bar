from __future__ import annotations

import json
import re
import uuid


class IncrementalToolStream:
    """Separate visible text, reasoning, and tool markup without full buffering.

    Tool-capable chat templates commonly emit ordinary text first and only add
    ``<tool_call>`` if a call is actually needed. Keeping a short possible-tag
    suffix is enough to stream normal output immediately while ensuring that a
    split tool marker is never exposed as assistant content.
    """

    # Every opening marker the runtime tool parsers recognise. Detecting only
    # `<tool_call>` let the other dialects stream to the client as ordinary
    # assistant text *and* be parsed into tool calls afterwards, so the caller
    # saw the raw markup and the call.
    TOOL_START = (
        "<tool_call>", "<|tool_call>", "<tool_call|>",
        "<|tool_call_start|>", "<|tool_list_start|>",
        "<atem:function_calls>", "<longcat_tool_call>", "<minimax:tool_call>",
        "<start_function_call>", "<|START_ACTION|>",
    )
    THINK_START = "<think>"
    THINK_END = "</think>"
    ASSISTANT_TAGS = ("<assistant>", "</assistant>")

    def __init__(self):
        self.pending = ""
        self.in_reasoning = False
        self.tool_detected = False

    def start_reasoning(self) -> None:
        self.in_reasoning = True

    def feed(self, text: str) -> list[dict]:
        if not text or self.tool_detected:
            return []
        self.pending += text
        events: list[dict] = []
        while self.pending and not self.tool_detected:
            markers = ((self.THINK_END, *self.TOOL_START) if self.in_reasoning else
                       (*self.TOOL_START, self.THINK_START, self.THINK_END, *self.ASSISTANT_TAGS))
            match = self._first_marker(self.pending, markers)
            if match is None:
                safe = self._safe_prefix_length(self.pending, markers)
                if safe:
                    events.append({"type": "reasoning_delta" if self.in_reasoning else "delta",
                                   "text": self.pending[:safe]})
                    self.pending = self.pending[safe:]
                break
            position, marker = match
            if position:
                events.append({"type": "reasoning_delta" if self.in_reasoning else "delta",
                               "text": self.pending[:position]})
            self.pending = self.pending[position + len(marker):]
            if marker in self.TOOL_START:
                self.tool_detected = True
            elif marker == self.THINK_START:
                self.in_reasoning = True
            elif marker == self.THINK_END:
                self.in_reasoning = False
            # Assistant wrapper tags are discarded.
        return [event for event in events if event.get("text")]

    def finish(self) -> list[dict]:
        if self.tool_detected or not self.pending:
            self.pending = ""
            return []
        event = {"type": "reasoning_delta" if self.in_reasoning else "delta", "text": self.pending}
        self.pending = ""
        return [event]

    @staticmethod
    def _first_marker(text: str, markers: tuple[str, ...]) -> tuple[int, str] | None:
        matches = [(text.find(marker), marker) for marker in markers if marker in text]
        return min(matches, key=lambda item: item[0]) if matches else None

    @staticmethod
    def _safe_prefix_length(text: str, markers: tuple[str, ...]) -> int:
        hold = 0
        for marker in markers:
            maximum = min(len(text), len(marker) - 1)
            for length in range(maximum, 0, -1):
                if text.endswith(marker[:length]):
                    hold = max(hold, length)
                    break
        return len(text) - hold


class StopSequenceFilter:
    """Cut generated text at an OpenAI `stop` sequence, split-safe.

    A stop string can straddle two deltas, so the same short-suffix holdback
    `IncrementalToolStream` uses for tool markers applies here: emit everything
    that cannot still turn into a stop sequence, and keep the rest until the
    next delta decides it.
    """

    def __init__(self, sequences):
        if isinstance(sequences, str):
            sequences = [sequences]
        self.sequences = tuple(item for item in (sequences or [])
                               if isinstance(item, str) and item)
        self.pending = ""
        self.hit = False

    def __bool__(self) -> bool:
        return bool(self.sequences)

    def feed(self, text: str) -> tuple[str, bool]:
        """Return (text safe to emit, whether a stop sequence ended output)."""
        if not self.sequences or self.hit:
            return ("" if self.hit else text), self.hit
        self.pending += text
        match = IncrementalToolStream._first_marker(self.pending, self.sequences)
        if match is not None:
            position, _ = match
            visible, self.pending, self.hit = self.pending[:position], "", True
            return visible, True
        safe = IncrementalToolStream._safe_prefix_length(self.pending, self.sequences)
        visible, self.pending = self.pending[:safe], self.pending[safe:]
        return visible, False

    def finish(self) -> str:
        visible, self.pending = ("" if self.hit else self.pending), ""
        return visible


def tool_template_kwargs_attempts(params: dict) -> list[dict]:
    """Ordered fallback kwargs for rendering a chat template that may not support tools.

    Some chat templates reject `tool_choice` outright, others reject `tools`
    entirely -- often deep inside Jinja2 rendering (an UndefinedError or
    TemplateError, not a clean TypeError) rather than at the Python call
    boundary. Try the most capable combination first, then progressively
    drop tool-calling kwargs so a template that simply doesn't support tools
    doesn't take the whole generation down with it.
    """
    template_kwargs = dict(params.get("chat_template_kwargs") or {})
    tools = params.get("tools")
    attempts = []
    tool_choice = params.get("tool_choice")
    if tools and tool_choice is not None:
        attempts.append({**template_kwargs, "tools": tools, "tool_choice": tool_choice})
    if tools:
        attempts.append({**template_kwargs, "tools": tools})
    attempts.append(template_kwargs)
    # OpenAI clients use high/minimal while Qwen's template uses xhigh/low.
    # Keep the client spelling as the first attempt for generic templates and
    # only try the Qwen-equivalent spelling after that rendering fails.
    aliases = {"high": "xhigh", "minimal": "low"}
    expanded = []
    for attempt in attempts:
        expanded.append(attempt)
        effort = attempt.get("reasoning_effort")
        alias = aliases.get(effort.casefold()) if isinstance(effort, str) else None
        if alias:
            expanded.append({**attempt, "reasoning_effort": alias})
    return expanded


def _argument(value: str):
    value = value.strip()
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _call(name: str, arguments) -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return {"id": f"call_{uuid.uuid4().hex}", "type": "function",
            "function": {"name": name.strip(), "arguments": arguments}}


def parse_tool_markup(text: str) -> dict:
    """Parse JSON, Laguna, and Qwen tool-call markup into OpenAI calls."""
    calls = []
    pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
    matches = pattern.findall(text)
    for raw in matches:
        raw = raw.strip()
        try:
            parsed = json.loads(raw)
            values = parsed if isinstance(parsed, list) else [parsed]
            for value in values:
                if not isinstance(value, dict):
                    continue
                function = value.get("function", value)
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    calls.append(_call(function["name"], function.get("arguments", {})))
            continue
        except json.JSONDecodeError:
            pass

        function_match = re.match(r"<function=([^>]+)>(.*?)(?:</function>)?$", raw, re.DOTALL)
        if function_match:
            name, body = function_match.groups()
            arguments = {key.strip(): _argument(value) for key, value in re.findall(
                r"<parameter=([^>]+)>\s*(.*?)\s*</parameter>", body, re.DOTALL
            )}
            calls.append(_call(name, arguments))
            continue

        name_match = re.match(r"([^<\s]+)(.*)$", raw, re.DOTALL)
        if name_match and "<arg_key>" in raw:
            name, body = name_match.groups()
            arguments = {key.strip(): _argument(value) for key, value in re.findall(
                r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", body, re.DOTALL
            )}
            calls.append(_call(name, arguments))

    remaining = pattern.sub(" ", text) if calls else text
    remaining = re.sub(r"<think>.*?</think>|</?(?:assistant|think)>", "", remaining,
                       flags=re.DOTALL).strip()
    return {"text": remaining, "tool_calls": calls}
