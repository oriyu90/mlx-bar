"""Worker-tier policy: how the rendered token sequence is banded.

The coordinator already applied the semantic (segment-level) policy. Here the
only decision left is a positional one on a flat token list: how many head
tokens (system / tool schema) and how many tail tokens (recent turns) stay
EXACT, leaving the middle band for the Writer.
"""

from __future__ import annotations

from dataclasses import dataclass

_HEAD_FRACTION = {"fidelity": 0.06, "balanced": 0.08, "memorySaver": 0.10}
_TAIL_FRACTION = {"fidelity": 0.60, "balanced": 0.45, "memorySaver": 0.30}
_HEAD_MAX = 512
_TAIL_MIN = 256
# Below this many tokens in the middle band the prefill saved is not worth a
# Writer pass or the fidelity hit.
MIN_BAND_TOKENS = 512


@dataclass(frozen=True)
class Banding:
    head: int
    band_start: int
    band_end: int
    tail: int

    @property
    def band_len(self) -> int:
        return self.band_end - self.band_start


def band(total_tokens: int, policy: str) -> Banding | None:
    if total_tokens < MIN_BAND_TOKENS * 2:
        return None
    head = min(_HEAD_MAX, max(1, int(total_tokens * _HEAD_FRACTION.get(policy, 0.08))))
    tail = max(_TAIL_MIN, int(total_tokens * _TAIL_FRACTION.get(policy, 0.45)))
    band_start, band_end = head, total_tokens - tail
    if band_end - band_start < MIN_BAND_TOKENS:
        return None
    return Banding(head=head, band_start=band_start, band_end=band_end, tail=tail)
