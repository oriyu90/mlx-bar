"""Metrics recorded for each adaptive-memory planning pass (DESIGN_v2.2.0.md §13)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class AdaptiveMetrics:
    raw_prompt_chars: int = 0
    effective_prompt_chars: int = 0
    exact_messages: int = 0
    latent_notes: int = 0
    cold_dropped: int = 0
    cold_source_chars: int = 0
    compression_ratio: float = 1.0
    policy_ms: float = 0.0
    writer_ms: float = 0.0
    latent_cache_hits: int = 0
    latent_cache_misses: int = 0
    policy: str = "balanced"
    policy_version: str = "am1"
    soft_token_requested: bool = False
    fallback_reason: str | None = None
    extra: dict = field(default_factory=dict)

    def as_summary(self) -> dict:
        data = asdict(self)
        data.pop("extra", None)
        data.update(self.extra)
        return data
