from __future__ import annotations

from typing import Any
from uuid import uuid4


PROTOCOL_VERSION = 1


def request(method: str, params: dict[str, Any] | None = None) -> dict:
    return {"protocol_version": PROTOCOL_VERSION, "request_id": str(uuid4()),
            "method": method, "params": params or {}}


def validate_message(message: dict) -> None:
    if message.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("unsupported worker protocol")
    if not isinstance(message.get("request_id"), str) or not isinstance(message.get("method"), str):
        raise ValueError("invalid worker message")
