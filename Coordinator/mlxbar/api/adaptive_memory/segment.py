"""Context Structure Analyzer -- classify a message's verbatim-risk.

Pure, deterministic, no I/O. A message is "verbatim-risk" when summarizing it
would plausibly lose information the model still needs literally: code, diffs,
shell commands, structured data, file paths, hashes/UUIDs, dense numbers, tool
traffic, or an image. Those are forced EXACT; everything else is a LATENT
candidate whose fate the policy decides.
"""

from __future__ import annotations

import re

_CODE_FENCE = re.compile(r"```|~~~")
_DIFF = re.compile(r"^(diff --git |@@ -\d|[+-]{3} [ab]/|Index: )", re.MULTILINE)
_SHELL = re.compile(r"(^|\n)\s*[$#>]\s?\S|\b(sudo|npm|pip|git|cargo|make|brew|python3?)\s", re.IGNORECASE)
_PATH = re.compile(r"(/[\w.-]+){2,}|[\w-]+\.(py|swift|ts|js|json|md|txt|sh|c|h|rs|go|toml|yaml|yml)\b")
_HASH = re.compile(r"\b[0-9a-f]{7,40}\b|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}", re.IGNORECASE)
_STRUCT = re.compile(r"[{}\[\]]|</?[a-zA-Z]|^\s*\"[\w-]+\"\s*:", re.MULTILINE)


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def has_image(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url" for part in content
    )


def _numeric_density(text: str) -> float:
    if not text:
        return 0.0
    digits = sum(character.isdigit() for character in text)
    return digits / len(text)


def is_tool_traffic(message: dict) -> bool:
    return message.get("role") == "tool" or bool(message.get("tool_calls"))


def is_verbatim_risk(message: dict) -> bool:
    """True when the message must stay EXACT under verbatim protection."""
    role = message.get("role")
    if role in {"system", "developer"}:
        return True
    if is_tool_traffic(message) or has_image(message):
        return True
    text = message_text(message)
    if not text:
        # No text and not an image/tool turn: nothing to compress, keep as-is.
        return True
    if _CODE_FENCE.search(text) or _DIFF.search(text):
        return True
    if _HASH.search(text) or _PATH.search(text):
        return True
    if _numeric_density(text) >= 0.12:
        return True
    if _SHELL.search(text) and len(text) < 2000:
        return True
    struct_hits = len(_STRUCT.findall(text))
    if struct_hits and struct_hits / max(1, len(text)) >= 0.02:
        return True
    return False


def classify(message: dict, *, verbatim_protection: bool) -> str:
    """Return "exact" or "latent" for a single message (COLD is a policy call)."""
    if verbatim_protection and is_verbatim_risk(message):
        return "exact"
    # Even with protection off, tool pairing and images must never be dropped
    # or summarized -- that would produce an invalid message sequence.
    if is_tool_traffic(message) or has_image(message) or message.get("role") in {"system", "developer"}:
        return "exact"
    if not message_text(message):
        return "exact"
    return "latent"
