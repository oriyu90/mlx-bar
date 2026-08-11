from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MLXBarError(Exception):
    code: str
    message: str
    status: int = 400
    retryable: bool = False

    def as_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            }
        }
