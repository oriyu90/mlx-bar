"""Worker-tier metrics (DESIGN_v2.2.0.md §13)."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class HybridMetrics:
    raw_prompt_tokens: int = 0
    effective_prompt_tokens: int = 0
    exact_tokens: int = 0
    latent_tokens: int = 0
    cold_source_tokens: int = 0
    compression_ratio: float = 1.0
    writer_ms: float = 0.0
    policy_ms: float = 0.0
    prefill_ms: float = 0.0
    latent_cache_hit: bool = False
    hybrid_kv_hit: bool = False
    writer_id: str = "meanpool-v1"
    fallback_reason: str | None = None

    def as_event(self) -> dict:
        return {"tier": "soft-token", **asdict(self)}
