from __future__ import annotations

import json
import re
import uuid


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
