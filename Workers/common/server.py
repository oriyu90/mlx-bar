from __future__ import annotations

import argparse
import asyncio
import contextlib
import gc
import json
import os
import re
import resource
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

from .tool_calls import IncrementalToolStream, StopSequenceFilter


GENERATION_HEARTBEAT_SECONDS = 10.0
# A cancel that lands after its generation already finished has nothing left to
# discard its id, so the set is bounded and aged instead of growing for the life
# of the worker process.
CANCEL_RETENTION_SECONDS = 900.0
CANCEL_MAX_ENTRIES = 256
# Re-measuring host memory on every token would cost more than it protects
# against; the pages this reads change on a far slower timescale.
HOST_MEMORY_CACHE_SECONDS = 2.0
MEMORY_CHECK_INTERVAL_SECONDS = 5.0
# The runtimes compute tokens-per-second as (n + 1) / elapsed-since-first-token,
# so the very first sample divides by roughly zero and reads in the tens of
# thousands. Wait for a few tokens before reporting anything.
MIN_PROGRESS_TOKENS = 3
VM_STAT_PATTERN = re.compile(r"^Pages\s+(free|inactive|speculative|purgeable):\s+(\d+)\.", re.MULTILINE)
_HOST_MEMORY_CACHE: dict[str, float] = {}


def host_memory() -> dict:
    """Available RAM and the OS's own memory-pressure verdict.

    `SC_AVPHYS_PAGES` does not exist on macOS, so free memory comes from
    `vm_stat`. `kern.memorystatus_vm_pressure_level` is what macOS itself uses
    to decide the machine is in trouble (1 normal, 2 warning, 4 critical) and
    accounts for everything else running, which a ratio against total RAM
    cannot.
    """
    now = time.monotonic()
    cached = _HOST_MEMORY_CACHE.get("at")
    if cached is not None and now - cached < HOST_MEMORY_CACHE_SECONDS:
        return dict(_HOST_MEMORY_CACHE["value"])
    result = {"available_bytes": 0, "pressure_level": 0, "process_rss_bytes": 0}
    try:
        output = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
                                capture_output=True, text=True, timeout=5).stdout
        result["process_rss_bytes"] = int(output.strip()) * 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        output = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True,
                                timeout=5).stdout
        pages = sum(int(value) for _, value in VM_STAT_PATTERN.findall(output))
        result["available_bytes"] = pages * page_size
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        output = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                capture_output=True, text=True, timeout=5).stdout
        result["pressure_level"] = int(output.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _HOST_MEMORY_CACHE.update(at=now, value=dict(result))
    return result


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
        # Why a request stopped, when the reason changes what cleanup is right.
        # A user cancelling wants the work so far kept; a memory watchdog
        # stopping the same request wants it released. Without the distinction
        # an adapter can only guess, and guessing wrong is either a wasted
        # prefill or a worker the OS kills.
        self._abort_reasons: dict[str, float] = {}
        self._abort_kinds: dict[str, str] = {}
        self._applied_memory_limits: dict[str, int] = {}

    def note_abort(self, request_id: str, reason: str) -> None:
        if not request_id:
            return
        self._abort_kinds[request_id] = reason
        self._abort_reasons[request_id] = time.monotonic()
        cutoff = time.monotonic() - CANCEL_RETENTION_SECONDS
        for key in [key for key, stamp in self._abort_reasons.items() if stamp < cutoff]:
            self._abort_reasons.pop(key, None)
            self._abort_kinds.pop(key, None)
        while len(self._abort_kinds) > CANCEL_MAX_ENTRIES:
            oldest = next(iter(self._abort_kinds))
            self._abort_kinds.pop(oldest, None)
            self._abort_reasons.pop(oldest, None)

    def abort_reason(self, request_id: str) -> str | None:
        return self._abort_kinds.get(request_id)

    def clear_abort(self, request_id: str) -> None:
        self._abort_kinds.pop(request_id, None)
        self._abort_reasons.pop(request_id, None)

    def capabilities(self) -> dict:
        return {"engine": self.engine, "protocolVersion": 1, "streaming": True,
                "modalities": ["text"], "loaded": self.model is not None,
                "memoryLimits": dict(self._applied_memory_limits)}

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

    def clear_prompt_cache(self) -> None:
        """Release optional cross-request state without unloading the model."""
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass

    def prompt_cache_stats(self) -> dict:
        """Return privacy-safe cache metadata for diagnostics."""
        return {"enabled": False, "engine": self.engine}

    def clear_disk_prompt_cache(self) -> None:
        """Clear optional persistent cache state owned by this worker."""
        return None

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
        # MLX's own counters miss everything else this process holds, so the
        # resident size is what bounds the real risk of being killed. It has to
        # be the *current* size: `ru_maxrss` is a high-water mark that never
        # falls, so one large prefill would keep every later request above the
        # limit for the life of the worker.
        with contextlib.suppress(Exception):
            result["peak_rss_bytes"] = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        result.update(host_memory())
        return result

    def apply_memory_limits(self) -> dict:
        """Pin the weights and cap MLX's allocator cache.

        Without an explicit wired limit macOS will page a large model's weights
        out under pressure, which turns a fast local model into a swap-bound
        one. Without a cache limit MLX's reuse pool grows against whatever is
        free, leaving nothing for the rest of the machine.
        """
        applied = {}
        try:
            import mlx.core as mx
        except Exception:
            return applied
        physical = 0
        with contextlib.suppress(AttributeError, OSError, ValueError):
            physical = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
        if physical <= 0:
            return applied
        # Unlike the pressure watchdog, set_memory_limit is enforced by MLX's
        # allocator.  In pool mode it is an absolute per-worker reservation,
        # not a copy of a machine-wide ratio.  Apply it before model.load().
        try:
            absolute = int(os.environ.get("MLXBAR_MLX_MEMORY_LIMIT_BYTES", "0"))
        except (TypeError, ValueError):
            absolute = 0
        memory_limit = getattr(mx, "set_memory_limit", None)
        if absolute > 0 and callable(memory_limit):
            memory_limit(absolute)
            applied["set_memory_limit"] = absolute
        for name, variable, default in (("set_wired_limit", "MLXBAR_WIRED_LIMIT_RATIO", 0.0),
                                        ("set_cache_limit", "MLXBAR_CACHE_LIMIT_RATIO", 0.0)):
            try:
                ratio = float(os.environ.get(variable, default))
            except (TypeError, ValueError):
                continue
            if ratio <= 0:
                continue
            function = getattr(mx, name, None)
            if not callable(function):
                continue
            # Older MLX builds raise when asked for a limit the kernel refuses;
            # the model still runs without it, so never fail the load over this.
            with contextlib.suppress(Exception):
                function(int(physical * ratio))
                applied[name] = int(physical * ratio)
        self._applied_memory_limits.update(applied)
        return applied

    def stream(self, request_id: str, params: dict) -> Iterator[dict]:
        raise NotImplementedError

    def count_tokens(self, params: dict) -> int:
        """Exact prompt token count for the same input `stream()` would run.

        Used by the Anthropic-compatible `count_tokens` endpoint. Adapters that
        cannot answer precisely raise NotImplementedError so the caller fails
        cleanly rather than returning a fabricated estimate.
        """
        raise NotImplementedError

    def finalize(self, text: str, params: dict) -> dict:
        """Convert buffered model output into public content/tool calls."""
        return {"text": text, "tool_calls": []}


def memory_pressure_reason(adapter: BaseAdapter, limit_ratio: float,
                           absolute_limit: int = 0) -> str | None:
    """Return a short reason when it is unsafe to keep generating.

    Shared by the worker's in-generation watchdog and the coordinator's
    pre-flight check so both judge pressure the same way.
    """
    memory = adapter.memory_stats()
    physical = int(memory.get("physical_memory_bytes", 0))
    if int(memory.get("pressure_level", 0)) >= 4:
        return "OSがメモリ逼迫を報告"
    used = max(int(memory.get("active_bytes", 0)) + int(memory.get("cache_bytes", 0)),
               int(memory.get("process_rss_bytes", 0)))
    if absolute_limit > 0 and used >= absolute_limit:
        return f"model reservation {absolute_limit} bytes exceeded"
    if physical <= 0:
        return None
    if limit_ratio > 0 and used / physical >= limit_ratio:
        return f"MLX使用量が物理メモリの{used / physical:.0%}"
    available = int(memory.get("available_bytes", 0))
    if limit_ratio > 0 and available > 0 and available < physical * (1 - limit_ratio):
        return f"空きメモリが{available / physical:.0%}"
    return None


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

    def close_on_mlx_thread(iterator) -> None:
        """Queue `iterator.close()` on the MLX thread without awaiting it.

        A disconnected client tears `lines()` down with GeneratorExit or
        CancelledError, and awaiting inside that teardown is unreliable -- the
        very next await can re-raise CancelledError and skip the close. The
        adapter generator must still be closed, or `stream_generate` keeps
        producing tokens for a client that is already gone and occupies the
        single MLX thread that every later request needs. Submitting the close
        instead of awaiting it guarantees it is queued; the executor's single
        worker runs it as soon as the in-flight `next_event` returns.
        """
        close = getattr(iterator, "close", None)
        if not callable(close):
            return

        def run() -> None:
            with contextlib.suppress(Exception):
                close()

        with contextlib.suppress(RuntimeError):
            mlx_executor.submit(run)

    @app.post("/rpc")
    async def rpc(message: dict):
        if message.get("protocol_version") != 1:
            return JSONResponse({"type": "error", "code": "PROTOCOL_MISMATCH",
                                 "message": "ワーカープロトコルのバージョンが一致しません",
                                 "detail": f"protocol_version={message.get('protocol_version')!r}",
                                 "retryable": False}, status_code=400)
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
            if method == "clear_prompt_cache":
                await on_mlx_thread(adapter.clear_prompt_cache)
                return {"type": "completed"}
            if method == "prompt_cache_stats":
                return {"type": "completed", "cache": await on_mlx_thread(adapter.prompt_cache_stats)}
            if method == "clear_disk_prompt_cache":
                await on_mlx_thread(adapter.clear_disk_prompt_cache)
                return {"type": "completed", "cache": await on_mlx_thread(adapter.prompt_cache_stats)}
            if method == "cancel":
                adapter.cancelled.add(params.get("request_id", ""))
                return {"cancelled": True, "forced": False}
            if method == "count_tokens":
                try:
                    count = await on_mlx_thread(adapter.count_tokens, params)
                except NotImplementedError:
                    return JSONResponse({"type": "error", "code": "COUNT_TOKENS_UNAVAILABLE",
                                         "message": "このランタイムはトークン数の計測に対応していません",
                                         "retryable": False}, status_code=501)
                return {"type": "completed", "input_tokens": int(count)}
            return JSONResponse({"type": "error", "code": "UNKNOWN_METHOD",
                                 "message": "不明なワーカーメソッドが要求されました",
                                 "detail": str(method), "retryable": False}, status_code=400)
        except ModuleNotFoundError as exc:
            return JSONResponse({"type": "error", "code": "RUNTIME_NOT_INSTALLED",
                                 "message": f"必要なランタイムがありません: {exc.name}", "retryable": False}, status_code=409)
        except Exception as exc:
            # The raw exception text is upstream (mlx-lm / mlx-vlm / transformers)
            # and English. Keep it in `detail` for logs and put a classified
            # Japanese sentence in `message`, which is what the GUI shows.
            text = str(exc)
            lowered = text.lower()
            if "remote code" in lowered:
                code, human = "MODEL_REQUIRES_REMOTE_CODE", "このモデルはリモートコードの実行が必要なため読み込めません"
            elif "out of memory" in lowered or "insufficient memory" in lowered or "metal" in lowered and "memory" in lowered:
                code, human = "MODEL_INCOMPATIBLE", "メモリ不足のためモデルを読み込めませんでした"
            elif "safetensors" in lowered or "no such file" in lowered or "not found" in lowered:
                code, human = "MODEL_INCOMPATIBLE", "モデルファイルを読み込めませんでした。ファイルが揃っているか確認してください"
            elif "chat template" in lowered or "chat_template" in lowered or "jinja" in lowered:
                code, human = "MODEL_INCOMPATIBLE", "チャットテンプレートの適用に失敗しました"
            else:
                code, human = "MODEL_INCOMPATIBLE", "モデルを読み込めませんでした"
            return JSONResponse({"type": "error", "code": code, "message": human,
                                 "detail": text[-1000:], "retryable": False}, status_code=400)

    @app.post("/generate")
    async def generate(message: dict):
        request_id = message.get("request_id", "")
        params = message.get("params") or {}

        async def lines():
            iterator = None
            try:
                try:
                    heartbeat_interval = min(30.0, max(0.01, float(
                        params.get("heartbeat_interval_seconds", GENERATION_HEARTBEAT_SECONDS))))
                except (TypeError, ValueError):
                    heartbeat_interval = GENERATION_HEARTBEAT_SECONDS
                yield json.dumps({"type": "phase", "name": "prefill", "message": "入力を処理中"}, ensure_ascii=False) + "\n"
                started = time.monotonic()
                last_visible_event = started
                try:
                    memory_limit_ratio = float(params.get("memory_limit_ratio", 0) or 0)
                except (TypeError, ValueError):
                    memory_limit_ratio = 0.0
                try:
                    memory_limit_bytes = max(0, int(params.get("memory_limit_bytes", 0) or 0))
                except (TypeError, ValueError):
                    memory_limit_bytes = 0
                last_memory_check = started
                count = 0
                upstream_metrics = {}
                buffered = ""
                tool_mode = bool(params.get("tools")) and params.get("tool_choice") != "none"
                # Always run the incremental splitter: with tools it also detects
                # `<tool_call>` markup; without tools it only separates
                # `<think>`/`</think>` so a reasoning model's chain-of-thought
                # never lands in `content` on a plain chat request.
                tool_stream = IncrementalToolStream(reasoning_only=not tool_mode)
                stop_filter = StopSequenceFilter(params.get("stop"))
                stopped = False
                live_tokens = 0
                live_tps = None
                first_delta_at = None
                last_progress = started
                prefill_estimate = None
                iterator = adapter.stream(request_id, params)
                while True:
                    pending = asyncio.create_task(on_mlx_thread(next_event, iterator))
                    while not pending.done():
                        done, _ = await asyncio.wait({pending}, timeout=heartbeat_interval)
                        if done:
                            break
                        elapsed = round(time.monotonic() - started, 1)
                        beat = {"type": "heartbeat", "phase": "prefill",
                                "elapsed_seconds": elapsed}
                        if prefill_estimate:
                            # A silent eight minutes and a silent eight minutes
                            # with "about 6 more to go" are the same wait, but
                            # only one of them is survivable.
                            beat["estimated_seconds"] = prefill_estimate
                            beat["remaining_seconds"] = max(0.0, round(prefill_estimate - elapsed, 1))
                        yield json.dumps(beat) + "\n"
                        last_visible_event = time.monotonic()
                    has_event, event = await pending
                    if not has_event:
                        break
                    # A long prompt plus a long reply grows the KV cache while
                    # this loop runs, so the coordinator's pre-flight check
                    # cannot be the only guard. Failing one request beats
                    # having the OS kill the whole worker.
                    if ((memory_limit_ratio > 0 or memory_limit_bytes > 0)
                            and time.monotonic() - last_memory_check >= MEMORY_CHECK_INTERVAL_SECONDS):
                        last_memory_check = time.monotonic()
                        exceeded = await on_mlx_thread(memory_pressure_reason,
                                                       adapter, memory_limit_ratio,
                                                       memory_limit_bytes)
                        if exceeded:
                            # Tell the adapter *why* before the generator is
                            # closed: its cleanup runs inside that close, and by
                            # then this frame is gone.
                            adapter.note_abort(request_id, "memory_pressure")
                            close_on_mlx_thread(iterator)
                            iterator = None
                            yield json.dumps({"type": "error", "code": "MEMORY_PRESSURE",
                                              "message": f"生成中にメモリ安全上限へ達したため停止しました（{exceeded}）",
                                              "retryable": True}, ensure_ascii=False) + "\n"
                            return
                    if event.get("type") == "prefill_estimate":
                        value = event.get("estimated_seconds")
                        if isinstance(value, (int, float)) and value > 0:
                            prefill_estimate = float(value)
                    if event.get("type") in {"delta", "token_progress"}:
                        # `token_progress` carries the same counters for tokens
                        # that produced no text: a runtime may hold a whole reply
                        # in its detokenizer and emit one delta at the very end,
                        # so counting deltas alone would report nothing.
                        # Read before the tool filter, which rebuilds events and
                        # would otherwise drop the counters on that path.
                        if first_delta_at is None:
                            first_delta_at = time.monotonic()
                        reported = event.get("tokens")
                        live_tokens = reported if isinstance(reported, int) else live_tokens + 1
                        rate = event.get("tps")
                        if isinstance(rate, (int, float)) and rate > 0:
                            live_tps = float(rate)
                        else:
                            # The runtime no longer reports a rate: measure it here
                            # so the display keeps working across runtime changes.
                            span = time.monotonic() - first_delta_at
                            live_tps = (live_tokens / span) if span > 0.05 else None
                    if (live_tokens >= MIN_PROGRESS_TOKENS
                            and time.monotonic() - last_progress >= heartbeat_interval):
                        # Deliberately does not touch `last_visible_event`: that
                        # timer drives the tool_parse heartbeat, which exists to
                        # keep the client's connection alive while tool markup is
                        # being buffered. Progress is a separate, status-only
                        # signal and must not starve it.
                        last_progress = time.monotonic()
                        yield json.dumps({"type": "progress", "generated_tokens": live_tokens,
                                          "generation_tps": round(live_tps, 2) if live_tps else None,
                                          "elapsed_seconds": round(last_progress - started, 1)}) + "\n"
                    if request_id in adapter.cancelled:
                        adapter.cancelled.discard(request_id)
                        close = getattr(iterator, "close", None)
                        if callable(close):
                            await on_mlx_thread(close)
                        yield json.dumps({"type": "completed", "finish_reason": "cancelled"}) + "\n"
                        return
                    if event.get("type") == "token_progress":
                        # Internal to the worker; `lines()` publishes its own
                        # throttled `progress` instead. Checked after
                        # cancellation, because a model that emits no text for a
                        # while makes these the only events in the loop.
                        continue
                    if event.get("type") == "reasoning_start":
                        tool_stream.start_reasoning()
                    elif event.get("type") == "delta":
                        count += 1
                        if tool_mode and not stopped:
                            # Anything past the stop sequence was never sent to
                            # the client, so it must not produce a tool call
                            # either.
                            buffered += str(event.get("text", ""))
                        # Without tools the splitter is in reasoning-only mode:
                        # it separates `<think>` from visible text and leaves
                        # any other markup untouched.
                        visible_events = tool_stream.feed(str(event.get("text", "")))
                        for visible in visible_events:
                            if stop_filter and visible.get("type") == "delta":
                                text, stopped = stop_filter.feed(str(visible.get("text", "")))
                                if not text:
                                    if stopped:
                                        break
                                    continue
                                visible = {**visible, "text": text}
                            yield json.dumps(visible, ensure_ascii=False) + "\n"
                            last_visible_event = time.monotonic()
                        if stopped:
                            break
                        if tool_mode and not visible_events and time.monotonic() - last_visible_event >= heartbeat_interval:
                            yield json.dumps({"type": "heartbeat", "phase": "tool_parse",
                                              "elapsed_seconds": round(time.monotonic() - started, 1)}) + "\n"
                            last_visible_event = time.monotonic()
                    elif event.get("type") == "metrics":
                        upstream_metrics.update(event)
                    else:
                        yield json.dumps(event, ensure_ascii=False) + "\n"
                        last_visible_event = time.monotonic()
                elapsed = max(time.monotonic() - started, 0.001)
                # `length` matters to clients deciding whether to continue, so
                # take the runtime's verdict rather than always claiming "stop".
                reported = upstream_metrics.get("finish_reason")
                finish_reason = reported if reported in {"stop", "length"} else "stop"
                if stopped:
                    finish_reason = "stop"
                if stop_filter and not stopped:
                    trailing = stop_filter.finish()
                    if trailing:
                        yield json.dumps({"type": "delta", "text": trailing}, ensure_ascii=False) + "\n"
                for visible in tool_stream.finish():
                    yield json.dumps(visible, ensure_ascii=False) + "\n"
                if tool_mode:
                    result = await on_mlx_thread(adapter.finalize, buffered, params)
                    if result.get("tool_calls"):
                        finish_reason = "tool_calls"
                        yield json.dumps({"type": "tool_calls", "calls": result["tool_calls"]}, ensure_ascii=False) + "\n"
                    elif tool_stream.tool_detected:
                        yield json.dumps({"type": "error", "code": "TOOL_PARSE_FAILED",
                                          "message": "モデルのtool callを解析できませんでした",
                                          "retryable": False}, ensure_ascii=False) + "\n"
                        return
                metrics = {"type": "metrics", "generation_tps": count / elapsed,
                           **upstream_metrics, "finish_reason": finish_reason}
                yield json.dumps(metrics) + "\n"
                completed_event = {"type": "completed", "finish_reason": finish_reason}
                if stop_filter and getattr(stop_filter, "matched", None):
                    # The Anthropic Messages path maps this to stop_reason
                    # "stop_sequence"; the OpenAI path ignores it.
                    completed_event["stop_sequence"] = stop_filter.matched
                yield json.dumps(completed_event, ensure_ascii=False) + "\n"
            except Exception as exc:
                # Upstream runtime exceptions are English; classify into a
                # Japanese `message` (shown by the GUI) and keep the raw text in
                # `detail` for the coordinator log.
                text = str(exc)
                lowered = text.lower()
                if "out of memory" in lowered or "insufficient memory" in lowered or (
                        "metal" in lowered and "memory" in lowered):
                    human = "メモリ不足のため生成できませんでした"
                elif "chat template" in lowered or "chat_template" in lowered or "jinja" in lowered:
                    human = "チャットテンプレートの適用に失敗しました"
                else:
                    human = "生成に失敗しました（ランタイムエラー）"
                yield json.dumps({"type": "error", "code": "GENERATION_FAILED", "message": human,
                                  "detail": text[-1000:], "retryable": False}, ensure_ascii=False) + "\n"
            finally:
                # Reached on normal completion, on error, and -- the case that
                # matters -- on the GeneratorExit/CancelledError raised when the
                # client disconnects mid-generation.
                adapter.cancelled.discard(request_id)
                if iterator is not None:
                    # Deliberately not awaited (see close_on_mlx_thread), so the
                    # adapter's own cleanup runs *after* this block returns. The
                    # abort reason must therefore outlive the request here; the
                    # registry expires and bounds itself instead.
                    close_on_mlx_thread(iterator)

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
