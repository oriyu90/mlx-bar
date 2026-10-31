"""Token fingerprinting and 401 diagnostics (v3.4.0).

Reported by an API operator (2026-10-31): a client that had cached a stale key
got the same 401 the server sends for every auth failure, and nothing in the
response let them tell "your key is stale" apart from "the server is down" or
"the model volume is unavailable". They had to put a logging proxy in front of
the port to discover that the key they were sending was not the current one.

Nothing here exposes the token. The fingerprint is the first 8 hex characters
of its SHA-256: enough for a client to compare against its own copy without
logging a secret, and useless for recovering the token.

Deliberately NOT implemented, after weighing the report's other suggestions:

* accepting the previous token for a grace period -- rotation exists to revoke
  a key, and the project's contract (and its e2e test) is that the old key stops
  working immediately. A 5-minute grace would keep a leaked key alive.
* serving ``/v1/models`` without a token -- the same contract requires it, and
  the catalog is the one thing this API does not hand out for free. The
  fingerprint plus ``/v1/auth/status`` give a client the same answer ("is my key
  current?") without opening the endpoint.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

FINGERPRINT_LENGTH = 8


def fingerprint(token: str) -> str:
    """Short, non-secret identifier for a token (first 8 hex of SHA-256)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def issued_at(settings) -> float | None:
    """When the current token file was written (its mtime)."""
    try:
        return Path(settings.token_path).stat().st_mtime
    except OSError:
        return None


def matches(header_value: str, token: str) -> bool:
    return secrets.compare_digest(header_value or "", "Bearer " + (token or ""))


def accept(settings, header_value: str) -> bool:
    """Whether ``Authorization`` names the current token.

    The single source of truth for both the ASGI listener wrapper and the
    FastAPI routes, so the two can never drift apart.
    """
    try:
        current = settings.api_token
    except (AttributeError, KeyError, OSError):
        return False
    return matches(header_value, current)


def failure_detail(settings) -> dict:
    """The 401 body: a stable code plus fields a client can compare.

    Deliberately does NOT echo the presented key back. A client compares
    ``key_fingerprint`` against the fingerprint of the key it holds; equal means
    the key is current and the problem is elsewhere.
    """
    try:
        current = settings.api_token
    except (AttributeError, KeyError, OSError):
        current = ""
    return {
        "code": "AUTHENTICATION_FAILED",
        "message": "APIキーが正しくありません。Authorization: Bearer ヘッダーを確認してください",
        "retryable": False,
        "retry_after_ms": 0,
        "key_fingerprint": fingerprint(current),
        "token_issued_at": issued_at(settings),
        "version": _version(),
    }


def _version() -> str:
    from . import __version__

    return __version__


def status_payload(settings) -> dict:
    """Body of the unauthenticated ``GET /v1/auth/status``."""
    required = bool(settings.data.get("api", {}).get("requireToken", True))
    return {
        "token_required": required,
        "key_fingerprint": fingerprint(settings.api_token) if required else None,
        "issued_at": issued_at(settings),
        "version": _version(),
    }


__all__ = ["fingerprint", "issued_at", "matches", "accept", "failure_detail",
           "status_payload", "time"]
