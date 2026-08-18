from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import platform
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import __version__
from ..errors import MLXBarError


router = APIRouter(prefix="/api/v1")


def state(request: Request):
    return request.app.state.mlxbar


@router.get("/health")
async def health(request: Request):
    return {"status": "ok", "version": __version__}


@router.get("/status")
async def status(request: Request):
    app = state(request)
    api = app.settings.data["api"]
    lan_enabled = app.settings.data["security"].get("allowLan", False)
    local_url = f"http://127.0.0.1:{api['port']}"
    lan_urls = [f"http://{address}:{api['port']}" for address in app.lan_ipv4_addresses()]
    return {"service": "running", **app.workers.status(),
            "api": {"enabled": api["enabled"], "host": api["host"],
                    "url": lan_urls[0] if lan_enabled and lan_urls else local_url,
                    "localUrl": local_url, "lanUrls": lan_urls if lan_enabled else [],
                    "lanEnabled": lan_enabled,
                    "error": app.public_listener_error}}


@router.get("/models")
async def models(request: Request):
    return {"data": state(request).database.list_models()}


@router.post("/models/scan", status_code=202)
async def scan(request: Request):
    return state(request).scan_job()


@router.post("/models/{model_id:path}/probe")
async def probe(model_id: str, request: Request):
    model = state(request).database.get_model(model_id)
    if not model:
        raise HTTPException(404, detail={"code": "MODEL_NOT_FOUND"})
    return {"compatible": model["format"] != "unknown", "model": model,
            "requiresRemoteCode": False}


@router.post("/models/{model_id:path}/load")
async def load(model_id: str, request: Request, body: dict = Body(default_factory=dict)):
    app = state(request)
    model = app.database.get_model(model_id)
    if not model:
        raise HTTPException(404, detail={"code": "MODEL_NOT_FOUND"})
    try:
        loaded = await app.workers.load(model, body.get("engine") if body.get("engine") != "auto" else None)
        app.database.set_metadata_value("last_loaded_model_id", model["id"])
        app.database.set_metadata_value("api_autoload_suspended", "0")
        return loaded
    except MLXBarError as exc:
        raise HTTPException(exc.status, detail=exc.as_dict()["error"])


@router.delete("/models/loaded")
async def unload(request: Request):
    app = state(request)
    result = await app.workers.unload()
    app.database.set_metadata_value("api_autoload_suspended", "1")
    return result


@router.post("/generate")
async def generate(request: Request, body: dict):
    async def stream():
        try:
            async for event in state(request).workers.generate(body.get("prompt", ""), body.get("images", []), body,
                                                                body.get("requestId")):
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        except MLXBarError as exc:
            yield "data: " + json.dumps({"type": "error", **exc.as_dict()["error"]}, ensure_ascii=False) + "\n\n"
        except Exception as exc:
            yield "data: " + json.dumps({"type": "error", "code": "INTERNAL_ERROR",
                                          "message": str(exc), "retryable": False}, ensure_ascii=False) + "\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/generate/cancel-all")
async def cancel_all(request: Request):
    return await state(request).workers.cancel_all()


@router.post("/generate/{request_id}/cancel")
async def cancel(request_id: str, request: Request):
    return await state(request).workers.cancel(request_id)


@router.get("/runtimes")
async def runtimes(request: Request):
    app = state(request)
    active_jobs = app.database.list_active_runtime_jobs()
    result = {}
    for engine in ("mlx-lm", "mlx-vlm"):
        saved_check = app.database.metadata_value(f"runtime_check:{engine}")
        try:
            last_check = json.loads(saved_check) if saved_check else None
        except (json.JSONDecodeError, TypeError):
            last_check = None
        result[engine] = {"active": app.slots.active(engine), "slots": app.slots.list(engine),
                          "history": app.database.list_runtime_history(engine, 10),
                          "activeJob": active_jobs.get(engine),
                          "lastCheck": last_check}
    return result


@router.post("/runtimes/{engine}/check")
async def runtime_check(engine: str, request: Request):
    if engine not in {"mlx-lm", "mlx-vlm"}:
        raise HTTPException(400, detail={"code": "INVALID_ENGINE"})
    try:
        result = await state(request).updater.check(engine)
        result["checkedAt"] = time.time()
        state(request).database.set_metadata_value(f"runtime_check:{engine}", json.dumps(result))
        return result
    except MLXBarError as exc:
        raise HTTPException(exc.status, detail=exc.as_dict()["error"])


@router.post("/runtimes/{engine}/update", status_code=202)
async def runtime_update(engine: str, request: Request):
    app = state(request)
    if engine not in {"mlx-lm", "mlx-vlm"}:
        raise HTTPException(400, detail={"code": "INVALID_ENGINE"})
    return app.runtime_update_job(engine)


@router.get("/runtimes/{engine}/history")
async def runtime_history(engine: str, request: Request):
    if engine not in {"mlx-lm", "mlx-vlm"}:
        raise HTTPException(400, detail={"code": "INVALID_ENGINE"})
    return {"data": state(request).database.list_runtime_history(engine, 50)}


@router.post("/runtimes/{engine}/stage", status_code=202)
async def runtime_stage(engine: str, request: Request, body: dict = Body(default_factory=dict)):
    app = state(request)
    if engine not in {"mlx-lm", "mlx-vlm"}:
        raise HTTPException(400, detail={"code": "INVALID_ENGINE"})
    async def work(update):
        result = await app.updater.stage(engine, update, body.get("version"), body.get("gitRef"))
        app.database.add_runtime_history(engine, result["slotId"], "staged", {
            "probe": result.get("probe", {}), "manual": True,
        })
        return result
    return app.jobs.create(f"runtime_stage:{engine}", work)


@router.delete("/runtimes/{engine}/slots/{slot_id}")
async def runtime_delete_slot(engine: str, slot_id: str, request: Request):
    app = state(request)
    if engine not in {"mlx-lm", "mlx-vlm"}:
        raise HTTPException(400, detail={"code": "INVALID_ENGINE"})
    if engine in app.database.list_active_runtime_jobs():
        raise HTTPException(409, detail={"code": "ENGINE_BUSY",
                                         "message": "ランタイム更新中はslotを削除できません"})
    try:
        result = app.slots.delete(engine, slot_id)
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "RUNTIME_DELETE_FAILED", "message": str(exc)})
    app.database.add_runtime_history(engine, slot_id, "deleted", result)
    return result


@router.post("/runtimes/{engine}/jobs/{job_id}/cancel")
async def runtime_cancel_job(engine: str, job_id: str, request: Request):
    app = state(request)
    if engine not in {"mlx-lm", "mlx-vlm"}:
        raise HTTPException(400, detail={"code": "INVALID_ENGINE"})
    job = app.database.get_job(job_id)
    if not job or job.get("kind") not in {f"runtime_update:{engine}", f"runtime_stage:{engine}"}:
        raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
    result = await app.jobs.cancel(job_id)
    return result


@router.post("/runtimes/{engine}/activate")
async def runtime_activate(engine: str, request: Request, body: dict):
    app = state(request)
    await app.workers.unload()
    try:
        return app.slots.activate(engine, body["slotId"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, detail={"code": "UPDATE_PROBE_FAILED", "message": str(exc)})


@router.post("/runtimes/{engine}/rollback")
async def runtime_rollback(engine: str, request: Request):
    app = state(request)
    await app.workers.unload()
    try:
        return app.slots.rollback(engine)
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "ROLLBACK_UNAVAILABLE", "message": str(exc)})


@router.get("/settings")
async def get_settings(request: Request):
    return state(request).settings.public()


@router.get("/settings/api-token")
async def get_api_token(request: Request):
    token = state(request).settings.api_token
    return {"token": token, "configured": bool(token)}


@router.put("/settings/api-token")
async def put_api_token(request: Request, body: dict):
    try:
        token = state(request).settings.set_api_token(body.get("token", ""))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, detail={"code": "INVALID_API_TOKEN", "message": str(exc)})
    return {"token": token, "configured": True}


@router.post("/settings/api-token/regenerate")
async def regenerate_api_token(request: Request):
    token = state(request).settings.regenerate_token()
    return {"token": token, "configured": True}


@router.get("/settings/lm-studio-token")
async def get_lm_studio_token(request: Request):
    token = state(request).settings.lm_studio_token
    return {"token": token or "", "configured": bool(token)}


@router.put("/settings/lm-studio-token")
async def put_lm_studio_token(request: Request, body: dict):
    try:
        token = state(request).settings.set_lm_studio_token(body.get("token"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, detail={"code": "INVALID_API_TOKEN", "message": str(exc)})
    return {"token": token or "", "configured": bool(token)}


@router.put("/settings")
async def put_settings(request: Request, body: dict):
    app = state(request)
    old_api = deepcopy(app.settings.data["api"])
    old_security = deepcopy(app.settings.data["security"])
    try:
        pending = app.settings.update(body)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "INVALID_SETTINGS", "message": str(exc)})
    new_api = pending["api"]
    listener_changed = (new_api["port"], new_api["host"]) != (old_api["port"], old_api["host"])
    if listener_changed and app.listener:
        try:
            await app.listener.switch(new_api["host"], new_api["port"])
            app.public_listener_error = None
        except Exception as exc:
            app.settings.update({"api": old_api, "security": old_security})
            raise HTTPException(409, detail={"code": "LISTENER_SWITCH_FAILED", "message": str(exc)})
    return app.settings.public()


@router.post("/settings/api-listener/test")
async def test_listener(request: Request, body: dict):
    app = state(request)
    return app.test_port(int(body.get("port", 0)), body.get("host", app.settings.data["api"]["host"]))


@router.get("/settings/api-listener/suggest")
async def suggest_listener(request: Request):
    port = state(request).next_port()
    return {"port": port}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    job = state(request).database.get_job(job_id)
    if not job:
        raise HTTPException(404, detail={"code": "JOB_NOT_FOUND"})
    return job


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request):
    async def stream():
        async for event in state(request).jobs.events(job_id):
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/diagnostics")
async def diagnostics(request: Request):
    app = state(request)
    masked_root = str(app.root).replace(str(Path.home()), "~")
    return {"version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
            "root": masked_root, "status": app.workers.status(), "settings": app.settings.public()}


@router.get("/logs")
async def recent_logs(request: Request, limit: int = 500):
    return {"retention": 2000, "data": state(request).database.list_api_logs(limit)}


@router.delete("/logs")
async def clear_logs(request: Request):
    return {"deleted": state(request).database.clear_api_logs()}
