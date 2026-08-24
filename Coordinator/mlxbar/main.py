from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import socket
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx
import secrets
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.management import router as management_router
from .api.openai_compat import router as public_router
from .settings import app_support_dir
from .state import AppState, CATALOG_CLASSIFIER_VERSION


LOGGER = logging.getLogger(__name__)

# Reachable without credentials so a monitor can see the listener is alive.
UNAUTHENTICATED_PATHS = {"/health"}
# Headroom above the largest request the settings actually permit.
REQUEST_SIZE_SLACK_BYTES = 1 << 20
DEFAULT_MAX_PROMPT_CHARACTERS = 100_000
DEFAULT_MAX_IMAGES = 8
DEFAULT_MAX_IMAGE_BYTES = 26_214_400
FALLBACK_MAX_REQUEST_BYTES = 512 << 20


def max_request_bytes(settings) -> int:
    """Largest request body the current settings could legitimately produce.

    A fixed cap would be wrong in both directions: eight 25 MiB images encoded
    as base64 data URIs is a *legal* request worth about 280 MB, while a small
    installation should not have to accept that much. Deriving the ceiling from
    the same limits the handlers enforce keeps the two from drifting apart.
    """
    data = getattr(settings, "data", {}) or {}
    try:
        configured = int(data.get("api", {}).get("maxRequestBytes", 0) or 0)
        if configured > 0:
            return configured
        generation = data.get("generation", {})
        # Four bytes per character is the UTF-8 worst case; base64 inflates by 4/3.
        prompt = int(generation.get("maxPromptCharacters", DEFAULT_MAX_PROMPT_CHARACTERS)) * 4
        images = (int(generation.get("maxImages", DEFAULT_MAX_IMAGES))
                  * int(generation.get("maxImageBytes", DEFAULT_MAX_IMAGE_BYTES)) * 4 // 3)
        return prompt + images + REQUEST_SIZE_SLACK_BYTES
    except (AttributeError, TypeError, ValueError):
        # This runs on every request. A malformed settings file must not turn
        # into a 500 for traffic that is otherwise fine; the handlers still
        # enforce their own limits, so falling back to a generous ceiling only
        # loses the early rejection.
        return FALLBACK_MAX_REQUEST_BYTES


class PublicRequestGuard:
    """Reject unauthorised or oversized requests before anything is parsed.

    FastAPI resolves a handler's `body: dict` parameter before the handler
    runs, so `authorize()` inside the handler was reached only after the whole
    request had been read and turned into Python objects. That let anyone who
    could reach the port allocate memory in proportion to what they sent,
    without presenting a credential. Rejecting at the ASGI layer keeps an
    unauthenticated request down to whatever the socket already buffered.

    Installed inside the access-log middleware so rejections are still logged.
    """

    def __init__(self, app, state):
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in UNAUTHENTICATED_PATHS:
            await self.app(scope, receive, send)
            return
        # First occurrence wins, matching what `request.headers.get()` returns
        # in the handler, so both layers judge the same value.
        headers: dict[str, str] = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.decode("latin-1").lower(), value.decode("latin-1"))
        if not self._authorized(headers):
            await self._reject(scope, send, 401, "AUTHENTICATION_FAILED",
                               "AUTHENTICATION_FAILED")
            return
        limit = max_request_bytes(self.state.settings)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, send, 413, "INPUT_TOO_LARGE",
                               f"要求が大きすぎます（上限{limit // 1_048_576}MB）")
            return
        await self.app(scope, _capped_receive(receive, limit), send)

    def _authorized(self, headers: dict) -> bool:
        try:
            if not self.state.settings.data["api"].get("requireToken", True):
                return True
            expected = "Bearer " + self.state.settings.api_token
        except (AttributeError, KeyError, OSError):
            # Anything unexpected about the token or its settings fails closed;
            # the handler's own authorize() then produces the real error.
            return False
        return secrets.compare_digest(headers.get("authorization", ""), expected)

    @staticmethod
    async def _reject(scope, send, status: int, code: str, message: str) -> None:
        # The access-log middleware reads this back off the shared scope state.
        scope.setdefault("state", {})["api_log"] = {"error_code": code}
        body = json.dumps({"error": {"message": message, "type": "invalid_request_error",
                                     "param": None, "code": code}}).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii")),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


def _capped_receive(receive, limit: int):
    """Stop feeding a body to the app once it exceeds `limit`.

    Covers requests that arrive chunked, where there is no Content-Length to
    check up front. The app sees a truncated body and rejects it as invalid;
    what matters here is that memory stays bounded.
    """
    total = 0

    async def capped():
        nonlocal total
        message = await receive()
        if message.get("type") == "http.request":
            total += len(message.get("body", b""))
            if total > limit:
                return {"type": "http.request", "body": b"", "more_body": False}
        return message

    return capped


def make_management_app(state: AppState) -> FastAPI:
    app = FastAPI(title="MLXBar Management API", docs_url=None, redoc_url=None)
    app.state.mlxbar = state
    app.include_router(management_router)

    @app.exception_handler(Exception)
    async def logged_internal_error(request: Request, exc: Exception):
        # Without this the caller gets a bare "Internal Server Error" and the
        # cause is written to a stream nobody keeps: the service log records
        # asyncio task failures but not request ones, so a route that starts
        # failing leaves no trace anywhere the user can reach.
        LOGGER.error("Management request failed: %s %s", request.method, request.url.path,
                     exc_info=exc)
        return JSONResponse(status_code=500, content={"error": {
            "code": "INTERNAL_ERROR", "message": type(exc).__name__}})

    return app


def make_public_app(state: AppState) -> FastAPI:
    app = FastAPI(title="MLXBar OpenAI API", docs_url=None, redoc_url=None)
    app.state.mlxbar = state

    @app.exception_handler(HTTPException)
    async def openai_http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        api_log = getattr(request.state, "api_log", None)
        if isinstance(api_log, dict):
            api_log["error_code"] = detail.get("code") or "HTTP_ERROR"
        if detail.get("code") or detail.get("parameters"):
            LOGGER.warning("OpenAI request rejected: code=%s parameters=%s path=%s",
                           detail.get("code"), detail.get("parameters"), request.url.path)
        error = {"message": detail.get("message") or detail.get("code") or "Request failed",
                 "type": detail.get("type", "invalid_request_error"),
                 "param": detail.get("param"), "code": detail.get("code")}
        if detail.get("parameters") is not None:
            error["parameters"] = detail["parameters"]
        if detail.get("retryable") is not None:
            error["retryable"] = detail["retryable"]
        return JSONResponse(status_code=exc.status_code, content={"error": error}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def openai_validation_error(_request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        location = first.get("loc", ())
        parameter = ".".join(str(item) for item in location if item != "body") or None
        return JSONResponse(status_code=422, content={"error": {
            "message": first.get("msg", "Invalid request body"),
            "type": "invalid_request_error", "param": parameter, "code": "INVALID_REQUEST",
        }})

    app.add_middleware(PublicRequestGuard, state=state)

    @app.middleware("http")
    async def recent_api_log(request: Request, call_next):
        started = time.monotonic()
        request.state.api_started_monotonic = started
        status = 500
        error_code = None
        recorded = False

        def record() -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            details = getattr(request.state, "api_log", {})
            client_host = request.client.host if request.client else ""
            try:
                state.database.add_api_log({
                    "request_id": details.get("request_id"), "method": request.method,
                    "path": request.url.path, "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "model": details.get("model"), "stream": details.get("stream", False),
                    "message_count": details.get("message_count", 0),
                    "tool_count": details.get("tool_count", 0),
                    "message_chars": details.get("message_chars", 0),
                    "tool_schema_chars": details.get("tool_schema_chars", 0),
                    "max_tokens": details.get("max_tokens", 0),
                    "reasoning_mode": details.get("reasoning_mode"),
                    "first_token_ms": details.get("first_token_ms"),
                    "prompt_tokens": details.get("prompt_tokens", 0),
                    "cached_tokens": details.get("cached_tokens", 0),
                    "prompt_tps": details.get("prompt_tps", 0),
                    "generation_tps": details.get("generation_tps", 0),
                    "cache_tier": details.get("cache_tier"),
                    # Why a request was cold, and how much of it was shared.
                    # The column, the writer, the worker and the settings window
                    # all had these from v1.6.0; this copy is what they travel
                    # through, and until v1.6.1 it was the one place they were
                    # not listed, so every row recorded a NULL reason.
                    "cold_reason": details.get("cold_reason"),
                    "shared_prefix_tokens": details.get("shared_prefix_tokens", 0),
                    "held_prefix_tokens": details.get("held_prefix_tokens", 0),
                    "tool_support": details.get("tool_support"),
                    "error_code": (details.get("error_code") or error_code
                                   or (f"HTTP_{status}" if status >= 400 else None)),
                    "client_scope": "local" if client_host in {"127.0.0.1", "::1", "testclient", ""} else "lan",
                })
            except Exception:
                pass
        try:
            response = await call_next(request)
            status = response.status_code
            original_iterator = getattr(response, "body_iterator", None)
            if original_iterator is not None:
                async def logged_body():
                    try:
                        async for chunk in original_iterator:
                            yield chunk
                    finally:
                        try:
                            closer = getattr(original_iterator, "aclose", None)
                            if closer is not None:
                                await closer()
                        finally:
                            record()
                response.body_iterator = logged_body()
            else:
                record()
            return response
        except Exception as exc:
            error_code = type(exc).__name__
            raise
        finally:
            if error_code:
                record()
    app.include_router(public_router)
    return app


class PublicListener:
    def __init__(self, state: AppState):
        self.state = state
        self.app = make_public_app(state)
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.host: str | None = None
        self.port: int | None = None
        # Retaining drain tasks keeps them from being garbage collected
        # mid-shutdown and surfaces their failures instead of discarding them.
        self.draining: set[asyncio.Task] = set()

    async def _launch(self, host: str, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
        listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # An update is quit, replace, launch -- and the port the previous
        # process was serving on is still in TIME_WAIT when the new one starts.
        # Without this the public API simply does not come back until something
        # restarts the service again, which is the worst possible first minute
        # after an update. SO_REUSEADDR only forgives that state: a port another
        # process is actively listening on still fails, which is what keeps the
        # conflict handling below meaningful.
        listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener_socket.bind((host, port))
            listener_socket.listen(128)
            listener_socket.setblocking(False)
        except OSError as exc:
            listener_socket.close()
            raise RuntimeError(f"port {port} を使用できません: {exc}") from exc
        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning",
                                access_log=False, timeout_graceful_shutdown=60, lifespan="off",
                                limit_concurrency=int(self.state.settings.data["api"].get(
                                    "maxConcurrentConnections", 64)))
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[listener_socket]))
        for _ in range(50):
            if server.started:
                try:
                    async with httpx.AsyncClient(timeout=0.5) as client:
                        response = await client.get(f"http://127.0.0.1:{port}/health")
                        if response.status_code == 200:
                            return server, task
                except Exception:
                    pass
            if task.done():
                break
            await asyncio.sleep(0.1)
        server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
        listener_socket.close()
        raise RuntimeError(f"port {port} でAPIを起動できません")

    async def start(self, host: str, port: int) -> None:
        self.server, self.task = await self._launch(host, port)
        self.host, self.port = host, port

    async def switch(self, host: str, port: int) -> None:
        async with self.lock:
            old_server, old_task = self.server, self.task
            old_host, old_port = self.host, self.port
            if old_server and old_task and old_port == port:
                old_server.should_exit = True
                await self._drain(old_task, timeout=10)
                try:
                    self.server, self.task = await self._launch(host, port)
                    self.host, self.port = host, port
                except Exception:
                    # A failed host change must not leave the public API down.
                    self.server, self.task = await self._launch(old_host or "127.0.0.1", old_port)
                    self.host, self.port = old_host, old_port
                    raise
                return
            new_server, new_task = await self._launch(host, port)
            self.server, self.task = new_server, new_task
            self.host, self.port = host, port
            if old_server and old_task:
                old_server.should_exit = True
                drain = asyncio.create_task(self._drain(old_task))
                self.draining.add(drain)
                drain.add_done_callback(self.draining.discard)

    @staticmethod
    async def _drain(task: asyncio.Task, timeout: int = 60) -> None:
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            task.cancel()

    async def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        if self.draining:
            await asyncio.gather(*list(self.draining), return_exceptions=True)


def configure_logging(root: Path) -> None:
    """Send coordinator diagnostics to a private per-user log file.

    launchd used to redirect stdout/stderr to a fixed path in world-writable
    /tmp, which another local account could pre-create as a symlink. Writing
    here keeps diagnostics inside the user's own Application Support directory.
    """
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "coordinator.log"
    with contextlib.suppress(OSError):
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(directory / "coordinator.log.1")
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _preload_last_model(state) -> "asyncio.Task | None":
    """Reload explicitly retained models and, optionally, the last model.

    Deliberately skipped when the user unloaded the model themselves: that flag
    is the difference between "MLXBar restarted" and "I turned this off", and
    only the first is an invitation to load 27 GB of weights unasked.
    """
    if state.database.metadata_value("api_autoload_suspended") == "1":
        return None
    ids = [str(profile.get("modelId"))
           for profile in state.settings.data.get("models", {}).get("pool", {}).get("profiles", [])
           if profile.get("keepLoaded") and profile.get("modelId")]
    if state.settings.data.get("general", {}).get("preloadLastModel", True):
        model_id = state.database.metadata_value("last_loaded_model_id")
        if model_id and model_id not in ids:
            ids.append(model_id)
    if not ids:
        return None

    async def work() -> None:
        try:
            async with state.model_autoload_lock:
                catalog = {item.get("id"): item for item in state.database.list_models()}
                for model_id in ids:
                    model = catalog.get(model_id)
                    if model is None:
                        continue
                    try:
                        await state.workers.load(model)
                        logging.getLogger(__name__).info(
                            "Preloaded %s", model.get("name") or model_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # One incompatible retained model must not block all
                        # remaining safe preloads.
                        logging.getLogger(__name__).warning(
                            "Could not preload %s: %s", model_id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A preload is a convenience. Failing it must not stop the
            # coordinator from serving, and the next request can still autoload.
            logging.getLogger(__name__).warning("Could not preload the last model: %s", exc)

    return asyncio.create_task(work())


async def serve(root: Path | None = None) -> None:
    state = AppState(root)
    configure_logging(state.root)
    control = state.root / "control"
    control.mkdir(parents=True, exist_ok=True)
    socket_path = control / "coordinator.sock"
    socket_path.unlink(missing_ok=True)
    # Scheduled before the management API can accept any connection (it only
    # *schedules* background jobs -- the actual install/scan work still runs
    # concurrently afterward, so this doesn't delay responsiveness). Doing
    # this earlier closes a race where a client could reach the socket and
    # call e.g. /system/reset while this synchronous setup was still
    # in-flight, tearing down state this was about to write to.
    state.install_missing_runtimes()
    if (state.database.metadata_value("catalog_classifier_version") != CATALOG_CLASSIFIER_VERSION
            or not state.database.list_models()
            or state.database.has_duplicate_model_paths()
            or state.database.has_mergeable_pathless_providers()):
        state.scan_job()
    listener = PublicListener(state)
    state.listener = listener
    if state.settings.data["api"].get("enabled", True):
        try:
            await listener.start(state.settings.data["api"]["host"], state.settings.data["api"]["port"])
        except RuntimeError as exc:
            state.public_listener_error = str(exc)
    management = uvicorn.Server(uvicorn.Config(make_management_app(state), uds=str(socket_path),
                                                log_level="warning", access_log=False,
                                                timeout_graceful_shutdown=10, lifespan="off"))
    state.management_server = management
    management_task = asyncio.create_task(management.serve())
    # Started only after the management API is serving, for the same reason the
    # runtime install is: a long synchronous step before the socket is live is a
    # window in which a client can reach half-built state. Loading a 27B model
    # takes tens of seconds, and the first request after a restart should not be
    # the thing that pays for it.
    preload_task = _preload_last_model(state)
    # launchd sends SIGTERM on logout, restart and `launchctl bootout`. Without
    # a handler the process dies before `finally` runs, stranding the worker
    # subprocess with its model still resident.
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(signal_name, management.handle_exit, signal_name, None)
    for _ in range(50):
        if socket_path.exists():
            os.chmod(socket_path, 0o600)
            break
        await asyncio.sleep(0.1)
    try:
        await management_task
    finally:
        if preload_task is not None:
            preload_task.cancel()
        # A runtime install (e.g. the automatic first-launch install above)
        # spawns `uv` in its own session so it can be killed by process
        # group; left alone, it survives this coordinator as an orphan still
        # writing into our data directory instead of being torn down with it.
        with contextlib.suppress(Exception):
            await state.jobs.cancel_all()
        with contextlib.suppress(Exception):
            await state.workers.unload()
        # `unload` only frees the model; the worker process itself must be
        # stopped too or it survives this coordinator as an orphan.
        with contextlib.suppress(Exception):
            await state.workers.shutdown()
        await listener.stop()
        socket_path.unlink(missing_ok=True)


def _emergency_log(root: Path | None, exc: BaseException) -> None:
    """Writes a crash record independent of the `logging` module's state.

    launchd's plists deliberately set no StandardErrorPath (a shared /tmp
    path is a symlink-attack vector), so Python's default excepthook -- and
    anything that throws before configure_logging() has run -- would
    otherwise vanish into /dev/null with zero trace. This writes with plain
    file I/O so it works even if logging itself is what failed to configure.
    Wrapped entirely in its own try/except so a failure here can never mask
    the original exception.
    """
    try:
        directory = (root or app_support_dir()) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "coordinator-crash.log"
        timestamp = datetime.now(timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"--- {timestamp} ---\n")
            handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            handle.write("\n")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except Exception:
        pass


def run() -> None:
    parser = argparse.ArgumentParser(description="MLXBar coordinator")
    parser.add_argument("--home", type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.home))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if logging.getLogger().handlers:
            logging.getLogger(__name__).exception("Coordinator crashed")
        _emergency_log(args.home, exc)
        # Re-raise so launchd sees a non-zero exit and its KeepAlive
        # (SuccessfulExit=false) restarts the service, instead of this
        # looking like a clean, intentional quit.
        raise SystemExit(1) from exc


if __name__ == "__main__":
    run()
