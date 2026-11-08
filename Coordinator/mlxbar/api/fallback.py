"""Management API for API fallback (v5.0.0, DESIGN_v5.0.0.md F-7).

Everything here is reachable only over the local management socket, never from
the public OpenAI/Anthropic surfaces: provider API keys must not be readable by
an API client. The document itself lives in ``config.json`` under
``apiFallback`` and is written through ``SettingsStore.update`` so the same
validation, atomic write and conflict handling apply as for every other
setting.

Ordering rule for deletes: the settings document is updated first and the key
file is removed only after that succeeds. The other order could leave a
document pointing at a provider whose key vanished.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, HTTPException, Request

from ..errors import MLXBarError
from ..fallback import client as fallback_client
from ..fallback.config import (
    MAX_MODELS_PER_PROVIDER,
    MAX_PROVIDERS,
    new_model_id,
    new_provider_id,
    normalise_model,
    normalise_provider,
    normalise_settings,
)
from ..fallback.selector import Candidate


router = APIRouter(prefix="/api/v1/fallback")
LOGGER_ERROR = "fallback settings request failed"


def _state(request: Request):
    return request.app.state.mlxbar


def _document(request: Request) -> dict:
    return normalise_settings(_state(request).settings.data.get("apiFallback"))


def _with_key_flags(request: Request, document: dict) -> dict:
    """Attach ``apiKeyConfigured`` from the key files, never the keys."""
    settings = _state(request).settings
    for provider in document.get("providers") or []:
        configured = False
        try:
            configured = settings.fallback_key_configured(provider.get("id", ""))
        except Exception:  # noqa: BLE001 - a read failure means "not configured"
            configured = False
        provider["apiKeyConfigured"] = configured
    return document


def _conflict() -> MLXBarError:
    return MLXBarError("SETTINGS_CONFLICT",
                       "設定の保存が別の操作と重なったため、今回は保存していません。"
                       "少し待ってからもう一度お試しください", 409, True)


def _read_only() -> MLXBarError:
    return MLXBarError("SETTINGS_READ_ONLY",
                       "この設定ファイルは新しいバージョンのMLXBarが作成したため、"
                       "このバージョンからは変更できません", 409, False)


async def _write(request: Request, document: dict) -> dict:
    """Persist the whole ``apiFallback`` subtree under the settings lock."""
    state = _state(request)
    settings = state.settings
    if getattr(settings, "forward_compatible", False):
        raise _read_only()
    try:
        await asyncio.wait_for(settings.write_lock.acquire(), timeout=5)
    except asyncio.TimeoutError as exc:
        raise _conflict() from exc
    try:
        try:
            settings.update({"apiFallback": document})
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_SETTINGS", "message": str(exc)})
    finally:
        settings.write_lock.release()
    return _with_key_flags(request, normalise_settings(settings.data.get("apiFallback")))


def _find_provider(document: dict, provider_id: str) -> dict:
    for provider in document.get("providers") or []:
        if provider.get("id") == provider_id:
            return provider
    raise HTTPException(404, detail={"code": "FALLBACK_PROVIDER_NOT_FOUND",
                                     "message": "指定されたフォールバックプロバイダーが見つかりません"})


def _find_model(provider: dict, model_id: str) -> dict:
    for model in provider.get("models") or []:
        if model.get("id") == model_id:
            return model
    raise HTTPException(404, detail={"code": "FALLBACK_MODEL_NOT_FOUND",
                                     "message": "指定されたフォールバックモデルが見つかりません"})


@router.get("")
async def get_fallback(request: Request):
    state = _state(request)
    return {"apiFallback": _with_key_flags(request, _document(request)),
            "status": state.fallback.status() if getattr(state, "fallback", None) else None}


@router.put("")
async def put_fallback(request: Request, body: dict = Body(default_factory=dict)):
    """Partial update of the ``apiFallback`` subtree.

    The body may carry any subset of the document's keys; nested ``providers``
    replace the whole array (the GUI edits providers through the dedicated
    routes below, which is what keeps ids stable). ``apiKeyConfigured`` is
    ignored on write -- it is derived from the key files.
    """
    document = _document(request)
    for key, value in body.items():
        if key == "providers":
            if not isinstance(value, list):
                raise HTTPException(422, detail={"code": "INVALID_SETTINGS",
                                                 "message": "providers must be an array"})
            # L-2: a whole-array write is a *merge*, not a replace. A client
            # that read the document before a provider was deleted must not
            # bring it back; it can still edit the providers it knows about.
            known = {provider.get("id") for provider in document.get("providers") or []}
            incoming = {item.get("id") for item in value if isinstance(item, dict)}
            resurrected = sorted(str(item) for item in incoming - known if item)
            if resurrected:
                raise HTTPException(409, detail={
                    "code": "SETTINGS_CONFLICT",
                    "message": "削除されたプロバイダーが設定に含まれています。"
                               "設定を読み直してからもう一度お試しください",
                    "providers": resurrected})
            by_id = {provider.get("id"): provider for provider in document.get("providers") or []}
            for item in value:
                if not isinstance(item, dict):
                    continue
                merged = normalise_provider({**by_id.get(item.get("id"), {}), **item})
                by_id[merged["id"]] = merged
            document["providers"] = list(by_id.values())
            continue
        if key not in document:
            raise HTTPException(422, detail={"code": "INVALID_SETTINGS",
                                             "message": f"unknown apiFallback key: {key}"})
        document[key] = value
    return {"apiFallback": await _write(request, document)}


@router.get("/status")
async def fallback_status(request: Request):
    state = _state(request)
    if getattr(state, "fallback", None) is None:
        return {"enabled": False, "providers": [], "lastOutcome": None, "recent": []}
    return state.fallback.status()


@router.post("/providers", status_code=201)
async def add_provider(request: Request, body: dict = Body(default_factory=dict)):
    document = _document(request)
    providers = document.get("providers") or []
    if len(providers) >= MAX_PROVIDERS:
        raise HTTPException(422, detail={"code": "INVALID_SETTINGS",
                                         "message": f"プロバイダーは最大{MAX_PROVIDERS}件です"})
    provider = normalise_provider({
        "id": new_provider_id(),
        "name": body.get("name") or "Provider",
        "protocol": body.get("protocol") or "openai-chat",
        "baseURL": body.get("baseURL") or "",
        "enabled": body.get("enabled", True),
        "timeoutSeconds": body.get("timeoutSeconds", 120),
        "models": [],
    })
    providers.append(provider)
    document["providers"] = providers
    saved = await _write(request, document)
    return {"apiFallback": saved, "providerId": provider["id"]}


@router.put("/providers/{provider_id}")
async def update_provider(provider_id: str, request: Request,
                          body: dict = Body(default_factory=dict)):
    document = _document(request)
    provider = _find_provider(document, provider_id)
    for key in ("name", "protocol", "baseURL", "enabled", "timeoutSeconds"):
        if key in body:
            provider[key] = body[key]
    provider["baseURL"] = str(provider.get("baseURL") or "").strip().rstrip("/")
    return {"apiFallback": await _write(request, document)}


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str, request: Request):
    document = _document(request)
    provider = _find_provider(document, provider_id)
    document["providers"] = [item for item in document.get("providers") or []
                             if item.get("id") != provider_id]
    saved = await _write(request, document)
    # Only after the document no longer references the provider.
    try:
        _state(request).settings.remove_fallback_api_key(provider.get("id", ""))
    except Exception:  # noqa: BLE001 - an orphan key file is harmless
        pass
    return {"apiFallback": saved}


@router.put("/providers/{provider_id}/api-key")
async def set_provider_key(provider_id: str, request: Request,
                           body: dict = Body(default_factory=dict)):
    document = _document(request)
    provider = _find_provider(document, provider_id)
    token = body.get("apiKey")
    if token is not None and not isinstance(token, str):
        raise HTTPException(422, detail={"code": "INVALID_API_TOKEN",
                                         "message": "APIキーは文字列で指定してください"})
    try:
        _state(request).settings.set_fallback_api_key(provider["id"], token)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, detail={"code": "INVALID_API_TOKEN", "message": str(exc)})
    return {"apiFallback": _with_key_flags(request, document)}


@router.post("/providers/{provider_id}/models", status_code=201)
async def add_model(provider_id: str, request: Request, body: dict = Body(default_factory=dict)):
    document = _document(request)
    provider = _find_provider(document, provider_id)
    models = provider.get("models") or []
    if len(models) >= MAX_MODELS_PER_PROVIDER:
        raise HTTPException(422, detail={"code": "INVALID_SETTINGS",
                                         "message": f"モデルは1プロバイダーあたり最大"
                                                    f"{MAX_MODELS_PER_PROVIDER}件です"})
    model = normalise_model({
        "id": new_model_id(),
        "model": body.get("model") or "",
        "label": body.get("label") or "",
        "enabled": body.get("enabled", True),
        "priority": body.get("priority", 10),
        "sourceModels": body.get("sourceModels", []),
        "sourceModelsExplicit": body.get("sourceModelsExplicit", False),
        "supportsImages": body.get("supportsImages", False),
        "maxRequests": body.get("maxRequests", 0),
        "rateWindowSeconds": body.get("rateWindowSeconds", 60),
        "maxOutputTokens": body.get("maxOutputTokens", 0),
        "maxContextTokens": body.get("maxContextTokens", 0),
        "imageDetail": body.get("imageDetail", "auto"),
        "temperature": body.get("temperature"),
        "topP": body.get("topP"),
        "extraHeaders": body.get("extraHeaders", {}),
    })
    models.append(model)
    provider["models"] = models
    saved = await _write(request, document)
    return {"apiFallback": saved, "modelId": model["id"]}


@router.put("/providers/{provider_id}/models/{model_id}")
async def update_model(provider_id: str, request: Request, model_id: str,
                       body: dict = Body(default_factory=dict)):
    document = _document(request)
    provider = _find_provider(document, provider_id)
    model = _find_model(provider, model_id)
    for key in ("model", "label", "enabled", "priority", "sourceModels",
                "sourceModelsExplicit", "supportsImages", "maxRequests",
                "rateWindowSeconds", "maxOutputTokens", "maxContextTokens",
                "imageDetail", "temperature", "topP", "extraHeaders"):
        if key in body:
            model[key] = body[key]
    merged = normalise_model(model)
    merged["id"] = model_id
    provider["models"] = [merged if item.get("id") == model_id else item
                          for item in provider.get("models") or []]
    return {"apiFallback": await _write(request, document)}


@router.delete("/providers/{provider_id}/models/{model_id}")
async def delete_model(provider_id: str, request: Request, model_id: str):
    document = _document(request)
    provider = _find_provider(document, provider_id)
    _find_model(provider, model_id)
    provider["models"] = [item for item in provider.get("models") or []
                          if item.get("id") != model_id]
    return {"apiFallback": await _write(request, document)}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, request: Request,
                        body: dict = Body(default_factory=dict)):
    """Send a one-token request to one model (or the provider's first model)."""
    state = _state(request)
    document = _document(request)
    provider = _find_provider(document, provider_id)
    models = [item for item in provider.get("models") or [] if item.get("enabled")]
    if not models:
        raise HTTPException(422, detail={"code": "INVALID_SETTINGS",
                                         "message": "テストするモデルが登録されていません"})
    model = models[0]
    wanted = body.get("modelId")
    if isinstance(wanted, str) and wanted:
        model = _find_model(provider, wanted)
    candidate = Candidate(
        provider_id=provider["id"], provider_name=provider.get("name") or "",
        protocol=provider.get("protocol") or "openai-chat",
        base_url=provider.get("baseURL") or "",
        timeout_seconds=int(provider.get("timeoutSeconds") or 120),
        model=model, provider=provider,
    )
    service = getattr(state, "fallback", None)
    api_key = state.settings.fallback_api_key(provider["id"])
    transport = getattr(service, "transport", None) if service else None
    result = await fallback_client.probe_provider(candidate, api_key=api_key,
                                                  transport=transport)
    return {"providerId": provider["id"], "modelId": model.get("id"),
            "model": model.get("model"), **result}


@router.post("/reset-limits")
async def reset_limits(request: Request):
    service = getattr(_state(request), "fallback", None)
    if service is not None:
        service.reset_limits()
    return {"ok": True}
