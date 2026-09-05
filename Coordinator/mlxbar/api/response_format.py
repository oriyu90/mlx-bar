"""``response_format`` support shared by the OpenAI and Anthropic-compat routes.

MLXBar has no grammar-constrained decoder, so both ``json_object`` and
``json_schema`` are implemented the same way real hosted APIs implement the
*weaker* of the two: a prompt instruction plus post-hoc validation of what the
model actually produced. Unlike OpenAI's ``json_schema`` mode (which
constrains decoding token-by-token and therefore cannot fail), this can fail --
the model can still emit prose or a shape that does not match. Failing loudly
with a retryable error is safer than returning content a caller assumes is
guaranteed-valid, silently, because that assumption is exactly what
``json_schema`` mode exists to provide.

The schema validator below is intentionally a small hand-rolled subset (no new
third-party dependency to bundle) covering the keywords a typical structured
extraction schema uses: ``type``, ``enum``, ``properties``/``required``/
``additionalProperties``, ``items``, and basic string/number bounds. Schemas
using anything outside that subset are rejected up front, at request time,
rather than silently ignored -- a caller relying on an unsupported keyword
(``oneOf``, ``$ref``, ...) needs to find out before it burns a generation.
"""

from __future__ import annotations

import json

_SUPPORTED_SCHEMA_KEYS = {
    "type", "enum", "const", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "minLength", "maxLength", "minimum",
    "maximum", "description", "title",
}
_SUPPORTED_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


class UnsupportedSchemaError(ValueError):
    """A schema uses a keyword or shape this validator cannot check."""


def check_schema_supported(schema: object, *, _path: str = "$") -> None:
    """Raise ``UnsupportedSchemaError`` if ``schema`` needs a keyword we don't validate.

    Called at request time so an unsupported schema is rejected before any
    generation happens, not discovered only when validation silently no-ops.
    """
    if not isinstance(schema, dict):
        raise UnsupportedSchemaError(f"{_path}: schema must be an object")
    unknown = set(schema) - _SUPPORTED_SCHEMA_KEYS
    if unknown:
        raise UnsupportedSchemaError(f"{_path}: unsupported schema keyword(s) {sorted(unknown)}")
    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not types or any(t not in _SUPPORTED_TYPES for t in types):
            raise UnsupportedSchemaError(f"{_path}.type: unsupported type {declared!r}")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise UnsupportedSchemaError(f"{_path}.properties must be an object")
        for key, sub in properties.items():
            check_schema_supported(sub, _path=f"{_path}.properties.{key}")
    items = schema.get("items")
    if items is not None:
        check_schema_supported(items, _path=f"{_path}.items")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise UnsupportedSchemaError(f"{_path}.additionalProperties must be a boolean")


def validate_json_schema(value: object, schema: dict, *, _path: str = "$") -> list[str]:
    """Return a list of human-readable violations; empty means ``value`` matches."""
    errors: list[str] = []
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else ([declared] if declared else None)
    if types and not any(_matches_type(value, t) for t in types):
        errors.append(f"{_path}: expected type {types}, got {_type_name(value)}")
        return errors  # further checks would only compound a type mismatch
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{_path}: value not in enum {schema['enum']!r}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{_path}: value != const {schema['const']!r}")
    if isinstance(value, dict) and (types is None or "object" in types):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{_path}: missing required property {key!r}")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                errors.append(f"{_path}: unexpected propert{'y' if len(extra) == 1 else 'ies'} {sorted(extra)}")
        for key, sub_schema in properties.items():
            if key in value:
                errors.extend(validate_json_schema(value[key], sub_schema, _path=f"{_path}.{key}"))
    if isinstance(value, list) and (types is None or "array" in types):
        min_items, max_items = schema.get("minItems"), schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{_path}: has {len(value)} items, minItems is {min_items}")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{_path}: has {len(value)} items, maxItems is {max_items}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_json_schema(item, item_schema, _path=f"{_path}[{index}]"))
    if isinstance(value, str):
        min_len, max_len = schema.get("minLength"), schema.get("maxLength")
        if isinstance(min_len, int) and len(value) < min_len:
            errors.append(f"{_path}: length {len(value)} < minLength {min_len}")
        if isinstance(max_len, int) and len(value) > max_len:
            errors.append(f"{_path}: length {len(value)} > maxLength {max_len}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{_path}: {value} < minimum {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{_path}: {value} > maximum {maximum}")
    return errors


def _matches_type(value: object, type_name: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "null":
        return value is None
    return False


def _type_name(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    return {dict: "object", list: "array", str: "string", int: "integer", float: "number"}.get(type(value), type(value).__name__)


def format_instruction(response_format: dict) -> str | None:
    """The prompt-level nudge to append for a given ``response_format``. ``None`` for plain text."""
    kind = response_format.get("type")
    if kind == "json_object":
        return ("Respond with a single valid JSON object only. "
                "No prose, no markdown code fences, no text before or after the JSON.")
    if kind == "json_schema":
        spec = response_format.get("json_schema") or {}
        schema = spec.get("schema")
        return ("Respond with a single valid JSON value that matches this JSON Schema exactly. "
                "No prose, no markdown code fences, no text before or after the JSON.\n"
                f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}")
    return None


def validate_output(text: str, response_format: dict | None) -> list[str]:
    """Violations of the requested ``response_format`` found in the generated text.

    Empty list means either there was no format requirement, or the text
    satisfies it. Never raises -- an unparseable response is itself a
    violation, not an exception, so a caller can uniformly report it.
    """
    if not response_format or response_format.get("type") not in {"json_object", "json_schema"}:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return ["response is not valid JSON"]
    if response_format.get("type") == "json_object" and not isinstance(parsed, dict):
        return ["response is valid JSON but not a JSON object"]
    if response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if isinstance(schema, dict):
            return validate_json_schema(parsed, schema)
    return []


def inject_into_messages(messages: list[dict], instruction: str) -> list[dict]:
    """Append ``instruction`` to the leading system message, or add one.

    Mirrors `context_compression`'s own invariant (index 0 is the system
    message when present) so the two features compose without either having
    to know about the other: whichever runs first, the instruction still ends
    up inside the message compression always keeps verbatim.
    """
    if messages and messages[0].get("role") == "system":
        updated = dict(messages[0])
        existing = updated.get("content")
        if isinstance(existing, str) and existing:
            updated["content"] = existing + "\n\n" + instruction
        else:
            updated["content"] = instruction
        return [updated, *messages[1:]]
    return [{"role": "system", "content": instruction}, *messages]
