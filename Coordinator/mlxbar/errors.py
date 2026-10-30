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

    def as_dict(self) -> dict:
        error = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.attempts:
            error["attempts"] = [dict(item) for item in self.attempts]
        return {"error": error}
