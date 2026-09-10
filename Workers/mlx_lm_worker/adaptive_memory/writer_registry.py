"""Writer registry -- id -> (callable, fingerprint).

One entry today (``meanpool-v1``). The indirection exists so a future learned
Writer can be slotted in without touching the runtime, and so cache keys carry
a Writer fingerprint that changes when the Writer does.
"""

from __future__ import annotations

from . import writer

_REGISTRY = {
    writer.WRITER_ID: (writer.soft_tokens, writer.fingerprint),
}

DEFAULT_WRITER_ID = writer.WRITER_ID


def get(writer_id: str):
    if writer_id not in _REGISTRY:
        raise KeyError(f"unknown writer {writer_id!r}")
    return _REGISTRY[writer_id]
