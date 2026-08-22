from __future__ import annotations

import argparse
import asyncio
import contextlib
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
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.management import router as management_router
from .api.openai_compat import router as public_router
from .settings import app_support_dir
from .state import AppState, CATALOG_CLASSIFIER_VERSION


LOGGER = logging.getLogger(__name__)


def make_management_app(state: AppState) -> FastAPI:
    app = FastAPI(title="MLXBar Management API", docs_url=None, redoc_url=None)
    app.state.mlxbar = state
    app.include_router(management_router)
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
                    "tool_support": details.get("tool_support"),
                    "error_code": details.get("error_code") or error_code,
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
        try:
            listener_socket.bind((host, port))
            listener_socket.listen(128)
            listener_socket.setblocking(False)
        except OSError as exc:
            listener_socket.close()
            raise RuntimeError(f"port {port} を使用できません: {exc}") from exc
        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning",
                                access_log=False, timeout_graceful_shutdown=60, lifespan="off")
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
