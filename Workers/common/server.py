from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn


GENERATION_HEARTBEAT_SECONDS = 10.0
# A cancel that lands after its generation already finished has nothing left to
# discard its id, so the set is bounded and aged instead of growing for the life
# of the worker process.
CANCEL_RETENTION_SECONDS = 900.0
CANCEL_MAX_ENTRIES = 256


class CancellationRegistry:
    """Bounded, self-expiring record of cancel requests by request id."""

    def __init__(self, retention: float = CANCEL_RETENTION_SECONDS,
                 maximum: int = CANCEL_MAX_ENTRIES):
        self._entries: dict[str, float] = {}
        self._retention = retention
        self._maximum = maximum

    def add(self, request_id: str) -> None:
        self._prune()
        self._entries[request_id] = time.monotonic()
        while len(self._entries) > self._maximum:
            self._entries.pop(next(iter(self._entries)))

    def discard(self, request_id: str) -> None:
        self._entries.pop(request_id, None)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._retention
        for key in [key for key, stamp in self._entries.items() if stamp < cutoff]:
            self._entries.pop(key, None)

    def __contains__(self, request_id: object) -> bool:
        self._prune()
        return request_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)


class BaseAdapter:
    engine = "unknown"

    def __init__(self):
        self.model: Any = None
        self.processor: Any = None
        self.cancelled = CancellationRegistry()

    def capabilities(self) -> dict:
        return {"engine": self.engine, "protocolVersion": 1, "streaming": True,
                "modalities": ["text"], "loaded": self.model is not None}

    def load(self, path: str, trust_remote_code: bool = False) -> dict:
        raise NotImplementedError

    def unload(self) -> None:
        self.model = None
        self.processor = None
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass

    def memory_stats(self) -> dict:
        result = {"active_bytes": 0, "cache_bytes": 0, "peak_bytes": 0}
        try:
            import mlx.core as mx
            for key, name in (("active_bytes", "get_active_memory"),
                              ("cache_bytes", "get_cache_memory"),
                              ("peak_bytes", "get_peak_memory")):
                function = getattr(mx, name, None)
                if callable(function):
                    result[key] = int(function())
        except Exception:
            pass
        try:
            result["physical_memory_bytes"] = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        except (AttributeError, OSError, ValueError):
            result["physical_memory_bytes"] = 0
        return result

    def stream(self, request_id: str, params: dict) -> Iterator[dict]:
        raise NotImplementedError

    def finalize(self, text: str, params: dict) -> dict:
        """Convert buffered model output into public content/tool calls."""
        return {"text": text, "tool_calls": []}


def create_app(adapter: BaseAdapter) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    # MLX keeps GPU stream state per thread.  Loading on asyncio.to_thread()
    # and later iterating StreamingResponse's synchronous generator on another
    # worker thread can therefore make an otherwise valid model fail with
    # "There is no Stream(...) in current thread".  Serialize every operation
    # that touches MLX through one dedicated worker thread.
    mlx_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"{adapter.engine}-mlx")

    async def on_mlx_thread(function, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(mlx_executor, function, *args)

    def next_event(iterator):
        try:
            return True, next(iterator)
        except StopIteration:
            return False, None

    @app.post("/rpc")
    async def rpc(message: dict):
        if message.get("protocol_version") != 1:
            return JSONResponse({"type": "error", "code": "PROTOCOL_MISMATCH",
                                 "message": "unsupported protocol", "retryable": False}, status_code=400)
        method = message.get("method")
        params = message.get("params") or {}
        try:
            if method == "health":
                return {"ok": True, "capabilities": adapter.capabilities()}
            if method == "capabilities":
                return adapter.capabilities()
            if method == "memory":
                return {"type": "completed", "memory": await on_mlx_thread(adapter.memory_stats)}
            if method == "load":
                result = await on_mlx_thread(adapter.load, params.get("path"), params.get("trust_remote_code", False))
                return {"type": "completed", "capabilities": result}
            if method == "unload":
                await on_mlx_thread(adapter.unload)
                return {"type": "completed"}
            if method == "cancel":
                adapter.cancelled.add(params.get("request_id", ""))
                return {"cancelled": True, "forced": False}
            return JSONResponse({"type": "error", "code": "UNKNOWN_METHOD", "message": str(method),
                                 "retryable": False}, status_code=400)
        except ModuleNotFoundError as exc:
            return JSONResponse({"type": "error", "code": "RUNTIME_NOT_INSTALLED",
                                 "message": f"必要なランタイムがありません: {exc.name}", "retryable": False}, status_code=409)
        except Exception as exc:
            text = str(exc)
            code = "MODEL_REQUIRES_REMOTE_CODE" if "remote code" in text.lower() else "MODEL_INCOMPATIBLE"
            return JSONResponse({"type": "error", "code": code, "message": text[-1000:],
                                 "retryable": False}, status_code=400)

    @app.post("/generate")
    async def generate(message: dict):
        request_id = message.get("request_id", "")
        params = message.get("params") or {}

        async def lines():
            try:
                try:
                    heartbeat_interval = min(30.0, max(0.01, float(
                        params.get("heartbeat_interval_seconds", GENERATION_HEARTBEAT_SECONDS))))
                except (TypeError, ValueError):
                    heartbeat_interval = GENERATION_HEARTBEAT_SECONDS
                yield json.dumps({"type": "phase", "name": "prefill", "message": "入力を処理中"}, ensure_ascii=False) + "\n"
                started = time.monotonic()
                last_visible_event = started
                count = 0
                buffered = ""
                tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
                iterator = adapter.stream(request_id, params)
                while True:
                    pending = asyncio.create_task(on_mlx_thread(next_event, iterator))
                    while not pending.done():
                        done, _ = await asyncio.wait({pending}, timeout=heartbeat_interval)
                        if done:
                            break
                        yield json.dumps({"type": "heartbeat", "phase": "prefill",
                                          "elapsed_seconds": round(time.monotonic() - started, 1)}) + "\n"
                        last_visible_event = time.monotonic()
                    has_event, event = await pending
                    if not has_event:
                        break
                    if request_id in adapter.cancelled:
                        adapter.cancelled.discard(request_id)
                        yield json.dumps({"type": "completed", "finish_reason": "cancelled"}) + "\n"
                        return
                    count += 1
                    if tool_mode and event.get("type") == "delta":
                        buffered += str(event.get("text", ""))
                        if time.monotonic() - last_visible_event >= heartbeat_interval:
                            yield json.dumps({"type": "heartbeat", "phase": "tool_parse",
                                              "elapsed_seconds": round(time.monotonic() - started, 1)}) + "\n"
                            last_visible_event = time.monotonic()
                    else:
                        yield json.dumps(event, ensure_ascii=False) + "\n"
                        last_visible_event = time.monotonic()
                elapsed = max(time.monotonic() - started, 0.001)
                finish_reason = "stop"
                if tool_mode:
                    result = await on_mlx_thread(adapter.finalize, buffered, params)
                    if result.get("text"):
                        yield json.dumps({"type": "delta", "text": result["text"]}, ensure_ascii=False) + "\n"
                    if result.get("tool_calls"):
                        finish_reason = "tool_calls"
                        yield json.dumps({"type": "tool_calls", "calls": result["tool_calls"]}, ensure_ascii=False) + "\n"
                yield json.dumps({"type": "metrics", "generation_tps": count / elapsed}) + "\n"
                yield json.dumps({"type": "completed", "finish_reason": finish_reason}) + "\n"
            except Exception as exc:
                yield json.dumps({"type": "error", "code": "GENERATION_FAILED", "message": str(exc)[-1000:],
                                  "retryable": False}, ensure_ascii=False) + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    return app


def run(adapter: BaseAdapter) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    args = parser.parse_args()
    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    app = create_app(adapter)

    @app.on_event("startup")
    async def restrict_socket() -> None:
        # Tighten the socket as soon as uvicorn has bound it. Doing this after
        # `uvicorn.run` returns would only ever run at shutdown, leaving the
        # socket at its default permissions for its entire useful life.
        try:
            os.chmod(args.socket, 0o600)
        except OSError:
            pass

    uvicorn.run(app, uds=args.socket, log_level="warning", access_log=False)
    try:
        os.unlink(args.socket)
    except (FileNotFoundError, OSError):
        pass
