from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MLXBarError(Exception):
    code: str
    message: str
    status: int = 400
    retryable: bool = False
    # v3.2.0 (WP-211): every engine the auto chain tried, in order, with its
    # own code/message. Additive -- absent (None) for errors raised on a single
    # code path, so existing `error` payloads keep their exact shape.
    attempts: list[dict] | None = field(default=None)
    # v3.4.0: how long a client should wait before retrying (ms). Additive, and
    # only set by errors that are actually worth retrying (a stalled model
    # volume, for instance) so the payload keeps its old shape elsewhere.
    retry_after_ms: int | None = field(default=None)

    def as_dict(self) -> dict:
        error = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.attempts:
            error["attempts"] = [dict(item) for item in self.attempts]
        if self.retry_after_ms is not None:
            error["retry_after_ms"] = int(self.retry_after_ms)
        return {"error": error}
