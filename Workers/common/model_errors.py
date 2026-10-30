"""Structured load failures shared by the workers (v3.2.0 / ISS-5).

The worker RPC classifies a raised exception into a `code` the coordinator
(and the GUI) can act on. Text sniffing in ``common/server.py`` cannot tell
"this artifact needs a bundled loader" from "the tokenizer file is missing",
so a load failure that already knows its cause raises this instead.

Codes owned by this module (additive; none replaces an older one):
``MODEL_NEEDS_BUNDLED_LOADER``, ``MODEL_FORMAT_UNSUPPORTED``,
``MODEL_QUANT_UNSUPPORTED``, ``MODEL_TOKENIZER_MISSING``,
``MODEL_WEIGHTS_INCOMPLETE`` (TASK-003: index が参照する重みファイルの
実在欠落。配置の平坦化など basename で吸収できない場合)。
"""
from __future__ import annotations


class ModelLoadError(Exception):
    """A load failure with a stable machine-readable cause."""

    def __init__(self, code: str, message: str, *,
                 detail: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "detail": self.detail, "retryable": self.retryable}
