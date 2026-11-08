"""Stored shape of ``apiFallback``: defaults, normalisation and validation.

The document lives in ``config.json`` under the top-level key ``apiFallback``
and is validated by ``SettingsStore._validate`` like every other setting, so the
GUI, the CLI and the management API cannot disagree about what is legal.

Secrets are *not* part of this shape. Each provider's API key is stored in
``control/fallback-<providerId>.key`` (mode 0600) exactly like the existing
``api-token`` / ``omlx-token`` files, and the document only carries
``apiKeyConfigured``. That flag is derived on read and stripped on write, so a
key can never be persisted into ``config.json`` by accident.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOLS = ("openai-chat", "openai-responses", "anthropic-messages")

#: ``*`` in ``sourceModels`` means "every MLXBar model".
WILDCARD = "*"

#: Provider ids are generated here and must be safe as a file name component.
PROVIDER_ID_RE = re.compile(r"^p-[0-9a-f]{8}$")
MODEL_ID_RE = re.compile(r"^m-[0-9a-f]{8}$")

MAX_PROVIDERS = 32
MAX_MODELS_PER_PROVIDER = 64
MAX_SOURCE_MODELS = 512
MAX_HEADERS = 32

DEFAULT_PROVIDER: dict[str, Any] = {
    "id": "",
    "name": "",
    "protocol": "openai-chat",
    "baseURL": "",
    "enabled": True,
    "timeoutSeconds": 120,
    "apiKeyConfigured": False,
    "models": [],
}

DEFAULT_MODEL: dict[str, Any] = {
    "id": "",
    "model": "",
    "label": "",
    "enabled": True,
    "priority": 10,
    # v5.0.0: an empty list means "no model is routed to this entry" -- the
    # user has to choose at least one, or "*" for every model. That keeps the
    # opt-in explicit: enabling the feature never silently captures requests.
    "sourceModels": [],
    # True when the user ticked "all models": the UI state, kept separate from
    # the resolved list so a newly scanned model is picked up without a save.
    "sourceModelsExplicit": False,
    "supportsImages": False,
    "maxRequests": 0,
    "rateWindowSeconds": 60,
    "maxOutputTokens": 0,
    "maxContextTokens": 0,
    "imageDetail": "auto",
    "temperature": None,
    "topP": None,
    "extraHeaders": {},
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    # A model that just failed is tried last for this long instead of being
    # dropped outright: if every candidate is cooling down, the oldest one is
    # still retried rather than failing the request outright.
    "cooldownSeconds": 60,
    # Upper bound on external models tried for one request.
    "maxAttempts": 3,
    # R9: with images attached, queueing behind a local model that can see them
    # is pointless when an external vision model is available, so the request
    # is forwarded instead of waiting for a queue slot.
    "forwardImagesImmediately": True,
    "providers": [],
}


def new_provider_id() -> str:
    return "p-" + uuid.uuid4().hex[:8]


def new_model_id() -> str:
    return "m-" + uuid.uuid4().hex[:8]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_http_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _normalise_header_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalise_model(raw: Any) -> dict[str, Any]:
    """Return a complete model entry with every default filled in.

    Unknown keys are dropped (the schema is closed, like every other setting),
    so a typo in a hand-edited config cannot silently change behaviour.
    """
    raw = raw if isinstance(raw, dict) else {}
    model = dict(DEFAULT_MODEL)
    for key in DEFAULT_MODEL:
        if key in raw:
            model[key] = raw[key]
    model["id"] = str(model.get("id") or "").strip()
    model["model"] = str(model.get("model") or "").strip()
    model["label"] = str(model.get("label") or "").strip()
    sources = model.get("sourceModels")
    if isinstance(sources, list):
        seen: list[str] = []
        for item in sources:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and text not in seen:
                seen.append(text)
        model["sourceModels"] = seen[:MAX_SOURCE_MODELS]
    else:
        model["sourceModels"] = []
    headers = model.get("extraHeaders")
    if isinstance(headers, dict):
        model["extraHeaders"] = {
            str(key): _normalise_header_value(value)
            for key, value in list(headers.items())[:MAX_HEADERS]
            if isinstance(key, str) and key.strip()
        }
    else:
        model["extraHeaders"] = {}
    return model


def normalise_provider(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    provider = dict(DEFAULT_PROVIDER)
    for key in DEFAULT_PROVIDER:
        if key in raw:
            provider[key] = raw[key]
    provider["id"] = str(provider.get("id") or "").strip()
    provider["name"] = str(provider.get("name") or "").strip()
    provider["baseURL"] = str(provider.get("baseURL") or "").strip().rstrip("/")
    provider["apiKeyConfigured"] = bool(provider.get("apiKeyConfigured", False))
    models = provider.get("models")
    provider["models"] = ([normalise_model(item) for item in models[:MAX_MODELS_PER_PROVIDER]]
                          if isinstance(models, list) else [])
    return provider


def normalise_settings(raw: Any) -> dict[str, Any]:
    """Fill in every default; never raises. Validation is separate on purpose.

    ``SettingsStore`` merges a stored document over ``DEFAULTS`` before
    validating, and a partially written ``apiFallback`` object must not make
    the merge produce a half-shaped document, so this runs on the way in.
    """
    raw = raw if isinstance(raw, dict) else {}
    settings = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in raw:
            settings[key] = raw[key]
    providers = settings.get("providers")
    settings["providers"] = ([normalise_provider(item) for item in providers[:MAX_PROVIDERS]]
                             if isinstance(providers, list) else [])
    return settings


def _validate_model(model: Any, where: str) -> None:
    if not isinstance(model, dict):
        raise ValueError(f"{where} must be an object")
    model_id = model.get("id")
    if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
        raise ValueError(f"{where}.id must look like m-xxxxxxxx")
    name = model.get("model")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where}.model must be a non-empty string")
    if len(name) > 256:
        raise ValueError(f"{where}.model must be at most 256 characters")
    label = model.get("label", "")
    if not isinstance(label, str) or len(label) > 256:
        raise ValueError(f"{where}.label must be a string of at most 256 characters")
    for key in ("enabled", "supportsImages", "sourceModelsExplicit"):
        if not isinstance(model.get(key), bool):
            raise ValueError(f"{where}.{key} must be boolean")
    priority = model.get("priority")
    if not _is_int(priority) or not 0 <= priority <= 1000:
        raise ValueError(f"{where}.priority must be an integer between 0 and 1000")
    sources = model.get("sourceModels")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCE_MODELS:
        raise ValueError(f"{where}.sourceModels must be an array with at most "
                         f"{MAX_SOURCE_MODELS} entries")
    seen: set[str] = set()
    for item in sources:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where}.sourceModels entries must be non-empty strings")
        if len(item) > 512:
            raise ValueError(f"{where}.sourceModels entries must be at most 512 characters")
        if item in seen:
            raise ValueError(f"{where}.sourceModels has a duplicate entry: {item}")
        seen.add(item)
    maximum = model.get("maxRequests")
    if not _is_int(maximum) or not 0 <= maximum <= 1_000_000:
        raise ValueError(f"{where}.maxRequests must be an integer between 0 and 1000000")
    window = model.get("rateWindowSeconds")
    if not _is_int(window) or not 10 <= window <= 86_400:
        raise ValueError(f"{where}.rateWindowSeconds must be between 10 and 86400")
    output = model.get("maxOutputTokens")
    if not _is_int(output) or not 0 <= output <= 1_000_000:
        raise ValueError(f"{where}.maxOutputTokens must be an integer between 0 and 1000000")
    context = model.get("maxContextTokens")
    if not _is_int(context) or not 0 <= context <= 10_000_000:
        raise ValueError(f"{where}.maxContextTokens must be an integer between 0 and 10000000")
    if model.get("imageDetail", "auto") not in {"auto", "low", "high"}:
        raise ValueError(f"{where}.imageDetail must be auto, low or high")
    for key in ("temperature", "topP"):
        value = model.get(key)
        if value is None:
            continue
        if not _is_number(value):
            raise ValueError(f"{where}.{key} must be a number or null")
        if key == "temperature" and not 0 <= float(value) <= 2:
            raise ValueError(f"{where}.temperature must be between 0 and 2")
        if key == "topP" and not 0 < float(value) <= 1:
            raise ValueError(f"{where}.topP must be greater than 0 and at most 1")
    headers = model.get("extraHeaders")
    if not isinstance(headers, dict) or len(headers) > MAX_HEADERS:
        raise ValueError(f"{where}.extraHeaders must be an object with at most "
                         f"{MAX_HEADERS} entries")
    for key, value in headers.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{where}.extraHeaders keys must be non-empty strings")
        if not isinstance(value, str):
            raise ValueError(f"{where}.extraHeaders values must be strings")
        if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
            raise ValueError(f"{where}.extraHeaders must not contain line breaks")


def validate_settings(raw: Any) -> None:
    """Raise ``ValueError`` with a key-naming message for an illegal document.

    The message always names the offending key so ``SettingsStore._recover_keys``
    can reset exactly that leaf instead of discarding the whole document.
    """
    if not isinstance(raw, dict):
        raise ValueError("apiFallback must be an object")
    for key in ("enabled", "forwardImagesImmediately"):
        if not isinstance(raw.get(key), bool):
            raise ValueError(f"apiFallback.{key} must be boolean")
    cooldown = raw.get("cooldownSeconds")
    if not _is_int(cooldown) or not 0 <= cooldown <= 86_400:
        raise ValueError("apiFallback.cooldownSeconds must be between 0 and 86400")
    attempts = raw.get("maxAttempts")
    if not _is_int(attempts) or not 1 <= attempts <= MAX_PROVIDERS * MAX_MODELS_PER_PROVIDER:
        raise ValueError("apiFallback.maxAttempts must be between 1 and 2048")
    providers = raw.get("providers")
    if not isinstance(providers, list) or len(providers) > MAX_PROVIDERS:
        raise ValueError(f"apiFallback.providers must be an array with at most {MAX_PROVIDERS} entries")
    provider_ids: set[str] = set()
    for index, provider in enumerate(providers):
        where = f"apiFallback.providers[{index}]"
        if not isinstance(provider, dict):
            raise ValueError(f"{where} must be an object")
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not PROVIDER_ID_RE.match(provider_id):
            raise ValueError(f"{where}.id must look like p-xxxxxxxx")
        if provider_id in provider_ids:
            raise ValueError(f"{where}.id is duplicated: {provider_id}")
        provider_ids.add(provider_id)
        name = provider.get("name")
        if not isinstance(name, str) or len(name) > 128:
            raise ValueError(f"{where}.name must be a string of at most 128 characters")
        if provider.get("protocol") not in PROTOCOLS:
            raise ValueError(f"{where}.protocol must be one of {', '.join(PROTOCOLS)}")
        base_url = provider.get("baseURL")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(f"{where}.baseURL must be an http(s) URL")
        if not _is_http_url(base_url) or len(base_url) > 2048:
            raise ValueError(f"{where}.baseURL must be an http(s) URL")
        if not isinstance(provider.get("enabled"), bool):
            raise ValueError(f"{where}.enabled must be boolean")
        if not isinstance(provider.get("apiKeyConfigured", False), bool):
            raise ValueError(f"{where}.apiKeyConfigured must be boolean")
        timeout = provider.get("timeoutSeconds")
        if not _is_int(timeout) or not 5 <= timeout <= 3600:
            raise ValueError(f"{where}.timeoutSeconds must be between 5 and 3600")
        models = provider.get("models")
        if not isinstance(models, list) or len(models) > MAX_MODELS_PER_PROVIDER:
            raise ValueError(f"{where}.models must be an array with at most "
                             f"{MAX_MODELS_PER_PROVIDER} entries")
        model_ids: set[str] = set()
        for model_index, model in enumerate(models):
            model_where = f"{where}.models[{model_index}]"
            _validate_model(model, model_where)
            model_id = model["id"]
            if model_id in model_ids:
                raise ValueError(f"{model_where}.id is duplicated: {model_id}")
            model_ids.add(model_id)


@dataclass(frozen=True)
class FallbackSettings:
    """A validated, read-only view of ``apiFallback`` for one request.

    Built from ``settings.data`` on every request (never latched) so a settings
    change applies without a restart, which is what the GUI implies.
    """

    enabled: bool = False
    cooldown_seconds: int = 60
    max_attempts: int = 3
    forward_images_immediately: bool = True
    providers: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def from_document(cls, raw: Any) -> "FallbackSettings":
        document = normalise_settings(raw)
        return cls(
            enabled=bool(document["enabled"]),
            cooldown_seconds=int(document["cooldownSeconds"]),
            max_attempts=int(document["maxAttempts"]),
            forward_images_immediately=bool(document["forwardImagesImmediately"]),
            providers=tuple(document["providers"]),
        )

    def active_providers(self) -> list[dict[str, Any]]:
        return [provider for provider in self.providers if provider.get("enabled")]

    def provider(self, provider_id: str) -> dict[str, Any] | None:
        for provider in self.providers:
            if provider.get("id") == provider_id:
                return provider
        return None

    def any_image_capable(self) -> bool:
        """Whether any enabled external model accepts images (R9 fast path)."""
        return any(model.get("supportsImages")
                   for provider in self.active_providers()
                   for model in provider.get("models", [])
                   if model.get("enabled"))

def prune_invalid_entries(raw: Any) -> dict[str, Any]:
    """Drop individual broken providers/models, keep everything valid.

    Called by ``SettingsStore._validate`` before the strict validation. A
    hand-edited ``config.json`` with one bad field must cost that one entry,
    not the whole list: the generic key recovery would otherwise reset
    ``apiFallback.providers`` to ``[]`` and silently delete every provider
    (review M-4). Structural problems (``providers`` not a list) are left for
    ``validate_settings`` to report, so the recovery resets exactly that key.
    """
    document = normalise_settings(raw)
    providers: list[dict[str, Any]] = []
    seen_providers: set[str] = set()
    for provider in document.get("providers") or []:
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not PROVIDER_ID_RE.match(provider_id) \
                or provider_id in seen_providers:
            continue
        models: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        for model in provider.get("models") or []:
            model_id = model.get("id")
            if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id) \
                    or model_id in seen_models:
                continue
            try:
                _validate_model(model, f"apiFallback.providers[{provider_id}].models")
            except ValueError:
                continue
            seen_models.add(model_id)
            models.append(model)
        provider["models"] = models
        seen_providers.add(provider_id)
        providers.append(provider)
    if isinstance(document.get("providers"), list):
        document["providers"] = providers
    return document
