"""Which external model may answer a request, and in what order.

The rules (DESIGN_v5.0.0.md F-2) are pure functions of the settings and the
request, so they can be tested without a server:

1. Only enabled providers and enabled models are candidates.
2. A model is a candidate for a request when one of its ``sourceModels`` names
   the requested MLXBar model (id, name or provider_key, case-insensitive), or
   when it lists ``*``.
3. **Explicit beats wildcard** (R5): when any candidate names the model
   explicitly, entries that only match through ``*`` are dropped. This is what
   makes "all models" a default rather than an override.
4. With images attached, only ``supportsImages`` models remain (R9). An empty
   result means "do not fall back" -- the local model keeps the images.
5. Order: ``priority`` ascending, then provider order, then model order.
6. Models that are cooling down after a failure, or that have hit their rate
   window, move to the back rather than being dropped: if every candidate is
   unavailable, the oldest one is still tried.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import FallbackSettings, WILDCARD


@dataclass(frozen=True)
class Candidate:
    """One external model, ready to be called."""

    provider_id: str
    provider_name: str
    protocol: str
    base_url: str
    timeout_seconds: int
    model: dict[str, Any]
    provider: dict[str, Any]

    @property
    def model_id(self) -> str:
        return str(self.model.get("id") or "")

    @property
    def model_name(self) -> str:
        return str(self.model.get("model") or "")

    @property
    def key(self) -> str:
        """Stable identity for rate/cooldown bookkeeping."""
        return f"{self.provider_id}/{self.model_id}"

    @property
    def display(self) -> str:
        label = str(self.model.get("label") or "").strip()
        name = label or self.model_name
        return f"{self.provider_name} / {name}" if self.provider_name else name


def _names_for(requested: str, aliases: Iterable[str] = ()) -> set[str]:
    """Every case-folded name a request model answers to."""
    values: set[str] = set()
    for raw in (requested, *aliases):
        if not isinstance(raw, str):
            continue
        text = raw.strip().casefold()
        if text:
            values.add(text)
    return values


def _matches(model: dict[str, Any], names: set[str]) -> tuple[bool, bool]:
    """Return ``(explicit, wildcard)`` for one model entry."""
    sources = model.get("sourceModels") or []
    explicit = False
    wildcard = False
    for source in sources:
        if not isinstance(source, str):
            continue
        text = source.strip()
        if text == WILDCARD:
            wildcard = True
        elif text.casefold() in names:
            explicit = True
    return explicit, wildcard


def select_candidates(
    settings: FallbackSettings,
    requested: str,
    *,
    has_images: bool = False,
    aliases: Iterable[str] = (),
    cooling: Iterable[str] = (),
    limited: Iterable[str] = (),
    limit: int | None = None,
) -> list[Candidate]:
    """Ordered external models that may answer ``requested``.

    ``cooling`` and ``limited`` are candidate keys (``providerId/modelId``) that
    should be tried last; they are never removed, so a request still has a
    destination when everything is busy.
    """
    if not settings.enabled:
        return []
    names = _names_for(requested, aliases)
    explicit_hits: list[tuple[int, int, Candidate]] = []
    wildcard_hits: list[tuple[int, int, Candidate]] = []
    provider_index = 0
    for provider in settings.providers:
        if not provider.get("enabled"):
            continue
        models = provider.get("models") or []
        for model_index, model in enumerate(models):
            if not model.get("enabled"):
                continue
            explicit, wildcard = _matches(model, names)
            if not explicit and not wildcard:
                continue
            if has_images and not model.get("supportsImages"):
                continue
            candidate = Candidate(
                provider_id=str(provider.get("id") or ""),
                provider_name=str(provider.get("name") or ""),
                protocol=str(provider.get("protocol") or "openai-chat"),
                base_url=str(provider.get("baseURL") or ""),
                timeout_seconds=int(provider.get("timeoutSeconds") or 120),
                model=model,
                provider=provider,
            )
            entry = (int(model.get("priority") or 0), provider_index * 1000 + model_index, candidate)
            (explicit_hits if explicit else wildcard_hits).append(entry)
        provider_index += 1
    # R5: an explicit assignment always wins over "all models".
    chosen = explicit_hits if explicit_hits else wildcard_hits
    chosen.sort(key=lambda item: (item[0], item[1]))
    ordered = [item[2] for item in chosen]
    cooling_set = set(cooling)
    limited_set = set(limited)

    def rank(candidate: Candidate) -> tuple[int, int]:
        penalty = 0
        if candidate.key in limited_set:
            penalty += 1
        if candidate.key in cooling_set:
            penalty += 2
        return penalty, 0

    # A stable sort keeps the priority order inside each penalty band.
    ordered.sort(key=rank)
    if limit is not None and limit > 0:
        return ordered[:limit]
    return ordered


def describe_sources(model: dict[str, Any]) -> str:
    """Human-readable source list, used by the status endpoint."""
    sources = [str(item) for item in (model.get("sourceModels") or [])]
    if WILDCARD in sources:
        explicit = [item for item in sources if item != WILDCARD]
        if explicit:
            return "*, " + ", ".join(explicit)
        return "*"
    return ", ".join(sources)
