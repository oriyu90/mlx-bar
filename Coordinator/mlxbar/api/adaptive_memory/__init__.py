"""Adaptive Hybrid Context Memory -- experimental (DESIGN_v2.2.0.md).

Long agent conversations are re-sent whole on every request. The prompt cache
already avoids re-prefilling them, but attention cost, the model's own context
window and KV memory still scale with the token count. ``contextCompression``
answers this by summarizing the middle into one synthetic ``system`` message;
this package answers it at *segment* granularity instead, routing each older
turn to one of three representations:

* **EXACT**  -- kept verbatim (system / tool schema / tool calls & results /
  code / diffs / shell / structured data / paths / hashes / numeric-dense
  text, plus the most recent ``keepTailMessages`` turns).
* **LATENT** -- natural-language discussion, replaced by a short cached note
  (coordinator tier) and optionally further compressed to soft tokens by the
  mlx-lm worker (``experimental.adaptiveMemory.softToken``).
* **COLD**   -- low-relevance turns dropped from inference entirely. The raw
  message is the source of record: a stateless client re-sends it, so nothing
  is lost -- it is simply not paid for on this turn.

The whole package is dormant unless ``experimental.adaptiveMemory.enabled``.
It shares ``contextCompression``'s best-effort contract:
``maybe_plan_adaptive_memory`` never raises and, on any doubt, returns the
input messages unchanged. It is mutually exclusive with ``contextCompression``
(``SettingsStore._validate`` rejects both being on).
"""

from __future__ import annotations

from .planner import POLICY_VERSION, adaptive_memory_worker_options, maybe_plan_adaptive_memory

__all__ = ["POLICY_VERSION", "adaptive_memory_worker_options", "maybe_plan_adaptive_memory"]
