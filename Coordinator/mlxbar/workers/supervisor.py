from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..errors import MLXBarError

WORKER_MODULES = {"mlx-lm": "mlx_lm_worker.adapter", "mlx-vlm": "mlx_vlm_worker.adapter"}
WORKER_LOG_MAX_BYTES = 1_048_576
WORKER_STARTUP_DETAIL_BYTES = 4000
LOGGER = logging.getLogger(__name__)


class WorkerStalled(Exception):
    """The worker produced neither a token nor a heartbeat for too long."""

    def __init__(self, seconds: float):
        super().__init__(f"worker stalled for {seconds}s")
        self.seconds = seconds


# Older than this and a reported rate says more about the past than the present.
PROGRESS_FRESHNESS_SECONDS = 30.0


@dataclass
class ActiveRequest:
    done: asyncio.Event
    cancel_requested: asyncio.Event
    task: asyncio.Task | None
    engine: str
    generated_tokens: int = 0
    generation_tps: float | None = None
    progress_at: float = 0.0


@dataclass
class QueuedRequest:
    cancel_requested: asyncio.Event
    enqueued_at: float


class WorkerSupervisor:
    def __init__(self, root: Path, settings):
        self.root = root
        self.settings = settings
        self.loaded: dict | None = None
        self.loading: dict | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.socket_path: Path | None = None
        self.engine: str | None = None
        self.active_requests: dict[str, ActiveRequest] = {}
        self.queued_requests: dict[str, QueuedRequest] = {}
        self.maintenance_engines: set[str] = set()
        self.crashes: list[float] = []
        self.lock = asyncio.Lock()
        self.generation_lock = asyncio.Lock()
        self.generation_owner: str | None = None
        self.generation_lock_recoveries = 0
        # Retained so fire-and-forget cancel notifications are not garbage
        # collected mid-flight.
        self._background_tasks: set[asyncio.Task] = set()
        self.reap_orphan_worker()

    @property
    def socket_dir(self) -> Path:
        # Scoped per coordinator root so one instance never sweeps away the
        # sockets of another (parallel test runs, a second MLXBAR_HOME).
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:8]
        return Path(tempfile.gettempdir()) / f"mlxbar-{os.getuid()}-{digest}"

    @property
    def manifest_path(self) -> Path:
        return self.root / "control" / "worker.json"

    def worker_log_path(self, engine: str) -> Path:
        return self.root / "logs" / f"worker-{engine}.log"

    def _write_manifest(self, engine: str, pid: int) -> None:
        try:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(json.dumps(
                {"pid": pid, "engine": engine, "socket": str(self.socket_path),
                 "startedAt": time.time()}), encoding="utf-8")
        except OSError:
            # A missing manifest only costs orphan detection on the next start;
            # it must never keep a healthy worker from coming up.
            pass

    def _clear_manifest(self) -> None:
        with contextlib.suppress(OSError):
            self.manifest_path.unlink(missing_ok=True)

    def reap_orphan_worker(self) -> dict | None:
        """Terminate a worker left behind by a coordinator that died abruptly.

        Nothing kills our children when this process is SIGKILLed, so a worker
        holding a multi-GB model can outlive the coordinator and keep its socket
        bound. launchd then restarts us with no memory of it. The manifest
        written at spawn time is what lets a fresh start find and clean it up.
        """
        result = None
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            pid = int(manifest["pid"])
        except (OSError, ValueError, TypeError, KeyError):
            manifest, pid = None, None
        if pid and pid > 1 and self._is_our_worker(pid):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(pid, sig)
                for _ in range(20):
                    if not self._process_alive(pid):
                        break
                    time.sleep(0.05)
                if not self._process_alive(pid):
                    break
            result = {"pid": pid, "engine": manifest.get("engine") if manifest else None}
        self._clear_manifest()
        self._sweep_sockets()
        return result

    @staticmethod
    def _process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _is_our_worker(pid: int) -> bool:
        """Confirm the recorded pid is still one of our worker modules.

        pids are recycled, so killing on the manifest alone could take out an
        unrelated process that happened to inherit the number.
        """
        try:
            output = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="],
                                    capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return any(module in output for module in WORKER_MODULES.values())

    def _sweep_sockets(self) -> None:
        current = str(self.socket_path) if self.socket_path else None
        try:
            entries = list(self.socket_dir.glob("*.sock"))
        except OSError:
            return
        for entry in entries:
            if str(entry) != current:
                with contextlib.suppress(OSError):
                    entry.unlink()

    def _open_worker_log(self, engine: str):
        path = self.worker_log_path(engine)
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.stat().st_size > WORKER_LOG_MAX_BYTES:
                path.unlink()
        handle = path.open("ab", buffering=0)
        os.chmod(path, 0o600)
        return handle

    async def _start_worker(self, engine: str) -> None:
        if self.process and self.process.returncode is None and self.engine == engine:
            return
        await self._stop_worker()
        socket_dir = self.socket_dir
        socket_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_dir, 0o700)
        self.socket_path = socket_dir / f"{engine}-{uuid.uuid4().hex[:8]}.sock"
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([*self._worker_import_paths(), env.get("PYTHONPATH", "")])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cache_settings = self.settings.data.get("promptCache", {})
        env["MLXBAR_PROMPT_CACHE_ROOT"] = str(self.root / "prompt-cache" / engine)
        env["MLXBAR_PROMPT_CACHE_DISK_ENABLED"] = (
            "1" if cache_settings.get("diskEnabled", True) else "0"
        )
        max_gb = min(100.0, max(1.0, float(cache_settings.get("diskMaxGB", 10))))
        env["MLXBAR_PROMPT_CACHE_MAX_BYTES"] = str(int(max_gb * (1 << 30)))
        env["MLXBAR_PROMPT_CACHE_KEEP_GENERATIONS"] = str(
            min(10, max(1, int(cache_settings.get("keepGenerations", 2)))))
        env["MLXBAR_PROMPT_CACHE_MEMORY_RATIO"] = str(cache_settings.get("memoryRatio", 0.10))
        # Stable block hashes are required for reuse across Python processes.
        # PromptCacheState owns the RAM tier, so do not duplicate exact hybrid
        # snapshots inside APC's separate in-memory LRU.
        env["APC_HASH"] = "sha256"
        env["APC_EXACT_CACHE_ENTRIES"] = "0"
        # Hybrid models can persist only exact prefix snapshots. Keep the final
        # 256 tokens cold so a different first user message can still reuse the
        # much larger, stable ZCode system/tools prefix.
        env["APC_EXACT_PREFIX_GUARD_TOKENS"] = "256"
        env["MLXBAR_PROMPT_CACHE_CHECKPOINT"] = (
            "1" if cache_settings.get("branchCheckpoint", "auto") == "auto" else "0"
        )
        write_budget_gb = max(0.0, float(cache_settings.get("diskWriteBudgetGB", 32)))
        env["MLXBAR_PROMPT_CACHE_WRITE_BUDGET_BYTES"] = str(int(write_budget_gb * (1 << 30)))
        # APC's in-memory block pool stays off unless asked for: its behaviour on
        # a 27B-class hybrid has not been measured, and a default that has not
        # been measured is a default that cannot be defended.
        env["MLXBAR_APC_MEMORY_BLOCKS"] = (
            "auto" if cache_settings.get("memoryBlocks", "off") == "auto" else "0"
        )
        generation = self.settings.data["generation"]
        env["MLXBAR_WIRED_LIMIT_RATIO"] = str(generation.get("wiredLimitRatio", 0.0))
        env["MLXBAR_CACHE_LIMIT_RATIO"] = str(generation.get("cacheLimitRatio", 0.0))
        # The worker decides on its own whether a snapshot is affordable, and it
        # has to judge pressure the same way the watchdog does.
        env["MLXBAR_MEMORY_LIMIT_RATIO"] = str(generation.get("memoryLimitRatio", 0.0))
        module = WORKER_MODULES.get(engine, WORKER_MODULES["mlx-vlm"])
        python = self._runtime_python(engine)
        # Worker diagnostics go to a log file rather than a pipe: nothing drains
        # a pipe once the worker is serving, so a chatty runtime would fill the
        # OS buffer and block the worker mid-generation with no crash signal.
        log_path = self.worker_log_path(engine)
        log = self._open_worker_log(engine)
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(python), "-m", module, "--socket", str(self.socket_path), env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=log,
            )
        finally:
            log.close()
        self.engine = engine
        self._write_manifest(engine, self.process.pid)
        for _ in range(50):
            if self.process.returncode is not None:
                detail = self._read_log_tail(log_path)
                await self._stop_worker()
                raise MLXBarError("WORKER_CRASHED", f"ワーカーを起動できません: {detail}", 503)
            if self.socket_path.exists():
                try:
                    response = await self._call("health", {})
                    if response.get("ok"):
                        return
                except Exception:
                    pass
            await asyncio.sleep(0.1)
        await self._stop_worker()
        raise MLXBarError("WORKER_CRASHED", "ワーカーの起動がタイムアウトしました", 503)

    @staticmethod
    def _read_log_tail(path: Path, limit: int = WORKER_STARTUP_DETAIL_BYTES) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - limit))
                return handle.read().decode(errors="replace").strip()
        except OSError:
            return "詳細を取得できませんでした"

    @staticmethod
    def _worker_import_paths() -> list[str]:
        candidates = []
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            candidates.extend([Path(frozen_root) / "Workers", Path(frozen_root)])
        source = Path(__file__).resolve()
        candidates.extend([source.parents[3] / "Workers", source.parents[2]])
        result: list[str] = []
        for candidate in candidates:
            value = str(candidate)
            if candidate.exists() and value not in result:
                result.append(value)
        return result

    def _runtime_python(self, engine: str) -> Path:
        active_path = self.root / "runtimes" / engine / "active.json"
        if active_path.exists():
            try:
                active = json.loads(active_path.read_text(encoding="utf-8")).get("active")
                candidate = self.root / "runtimes" / engine / "slots" / active / ".venv" / "bin" / "python"
                if candidate.exists():
                    return candidate
            except Exception:
                pass
        raise MLXBarError("RUNTIME_NOT_INSTALLED", f"{engine}ランタイムを先にインストールしてください", 409)

    async def _stop_worker(self) -> None:
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        self.process = None
        if self.socket_path:
            self.socket_path.unlink(missing_ok=True)
        self.socket_path = None
        self.engine = None
        self._clear_manifest()

    async def _call(self, method: str, params: dict, timeout: float | None = 30) -> dict:
        if not self.socket_path:
            raise MLXBarError("WORKER_CRASHED", "ワーカーが停止しています", 503)
        transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        async with httpx.AsyncClient(transport=transport, base_url="http://worker", timeout=timeout) as client:
            response = await client.post("/rpc", json={"protocol_version": 1, "request_id": str(uuid.uuid4()),
                                                        "method": method, "params": params})
            data = response.json()
            if response.status_code >= 400 or data.get("type") == "error":
                raise MLXBarError(data.get("code", "WORKER_ERROR"), data.get("message", "worker error"), 400,
                                  data.get("retryable", False))
            return data

    async def load(self, model: dict, engine: str | None = None) -> dict:
        chosen = engine or model.get("engine")
        model_max_tokens = self._detect_model_max_tokens(model.get("path"))
        if chosen not in {"mlx-lm", "mlx-vlm"}:
            if chosen != "lm-studio":
                raise MLXBarError("MODEL_INCOMPATIBLE", "利用可能なエンジンがありません", 409)
        async with self.lock:
            if (self.loaded and self.loaded.get("id") == model.get("id")
                    and self.loaded.get("engine") == chosen):
                return self.loaded
            self.loading = {"id": model.get("id"), "name": model.get("name"), "engine": chosen,
                            "phase": "準備中", "startedAt": time.time()}
            timeout = self.settings.data["generation"]["loadTimeoutSeconds"]
            try:
                if self.loaded:
                    self.loading["phase"] = "現在のモデルをアンロード中"
                    await self.unload()
                if chosen == "lm-studio":
                    self.loading["phase"] = "LM Studioへ接続中"
                    provider = await self._load_lmstudio(model, timeout)
                    self.loaded = {**model, "engine": chosen, "state": "loaded",
                                   "provider_instance_id": provider.get("instance_id"),
                                   "capabilities": {"modelMaxTokens": model_max_tokens}}
                    return self.loaded
                candidates = [chosen]
                if chosen == "mlx-lm":
                    # mlx-vlm includes native and compatibility implementations
                    # for some text-only architectures that mlx-lm does not.
                    candidates.append("mlx-vlm")
                last_error: MLXBarError | None = None
                for candidate in candidates:
                    chosen = candidate
                    self.loading["engine"] = chosen
                    self.loading["phase"] = ("mlx-vlmへ切り替え中" if last_error else "Workerを起動中")
                    try:
                        await self._start_worker(chosen)
                        self.loading["phase"] = "モデルデータを読み込み中"
                        result = await self._call("load", {"path": model.get("path"),
                                                            "trust_remote_code": False}, timeout=timeout)
                        break
                    except MLXBarError as exc:
                        await self._stop_worker()
                        last_error = exc
                        if exc.code != "MODEL_INCOMPATIBLE":
                            raise
                else:
                    if not model.get("provider_key"):
                        raise last_error or MLXBarError("MODEL_INCOMPATIBLE", "モデルをロードできません", 409)
                    chosen = "lm-studio"
                    self.loading["engine"] = chosen
                    self.loading["phase"] = "LM Studioへ切り替え中"
                    provider = await self._load_lmstudio(model, timeout)
                    self.loaded = {**model, "engine": chosen, "state": "loaded",
                                   "provider_instance_id": provider.get("instance_id"),
                                   "capabilities": {"modelMaxTokens": model_max_tokens}}
                    return self.loaded
            except (httpx.TimeoutException, TimeoutError) as exc:
                await self._stop_worker()
                raise MLXBarError("MODEL_LOAD_TIMEOUT", f"モデルのロードが{timeout}秒以内に完了しませんでした", 504, True) from exc
            except Exception:
                await self._stop_worker()
                raise
            finally:
                self.loading = None
            capabilities = {**result.get("capabilities", {}), "modelMaxTokens": model_max_tokens}
            self.loaded = {**model, "engine": chosen, "state": "loaded", "capabilities": capabilities}
            return self.loaded

    async def unload(self) -> dict:
        if not self.loaded:
            return {"state": "unloaded"}
        if self.loaded.get("engine") == "lm-studio":
            self.loaded = None
            return {"state": "unloaded"}
        try:
            # DiskBlockStore drains its background safetensors writer during
            # unload. Large hybrid snapshots can take longer than the old
            # five-second model-only shutdown budget.
            await asyncio.wait_for(self._call("unload", {}, timeout=30), timeout=30)
        except Exception:
            await self._stop_worker()
        self.loaded = None
        return {"state": "unloaded"}

    async def prompt_cache_stats(self) -> dict:
        if (not self.loaded or self.loaded.get("engine") == "lm-studio"
                or not self.socket_path):
            return {"enabled": False, "engine": self.loaded.get("engine") if self.loaded else None}
        try:
            response = await self._call("prompt_cache_stats", {}, timeout=10)
        except httpx.TimeoutException:
            # The Worker answers RPCs on the MLX thread, so a generation in
            # flight blocks this read for as long as it runs -- minutes on a
            # 27B. "Is reuse working?" is exactly the question a slow request
            # provokes, so answer it with what is known and say so, rather than
            # failing at the only moment anyone asks.
            known = ((self.loaded or {}).get("capabilities") or {}).get("promptCache") or {}
            return {**known, "stale": True, "staleReason": "worker_busy"}
        return response.get("cache", {})

    async def clear_memory_prompt_cache(self) -> dict:
        if (self.loaded and self.loaded.get("engine") != "lm-studio" and self.socket_path):
            await self._call("clear_prompt_cache", {}, timeout=10)
        return await self.prompt_cache_stats()

    async def clear_disk_prompt_cache(self) -> dict:
        if (self.loaded and self.loaded.get("engine") != "lm-studio" and self.socket_path):
            response = await self._call("clear_disk_prompt_cache", {}, timeout=30)
            return response.get("cache", {})
        root = self.root / "prompt-cache"
        if root.exists():
            import shutil
            shutil.rmtree(root)
        return {"enabled": False, "disk_bytes": 0}

    async def generate(self, prompt, images: list[str], options: dict, request_id: str | None = None,
                       image_root: Path | None = None):
        if not self.loaded:
            raise MLXBarError("MODEL_NOT_LOADED", "モデルがロードされていません", 409)
        if self.loaded.get("engine") in self.maintenance_engines:
            raise MLXBarError("ENGINE_BUSY", "ランタイム切替中のため新しい生成を開始できません", 409, True)
        prompt, images, options = self._validate_generation(prompt, images, options, image_root)
        request_id = request_id or str(uuid.uuid4())
        self._recover_orphaned_generation_slot("new_request")
        generation_acquired = False
        queued: QueuedRequest | None = None
        acquire_task: asyncio.Task | None = None
        cancel_task: asyncio.Task | None = None
        if self.generation_lock.locked() or self.queued_requests:
            limits = self.settings.data["generation"]
            if len(self.queued_requests) >= limits.get("maxQueuedRequests", 16):
                raise MLXBarError("QUEUE_FULL", "生成待ちが上限に達しています。しばらくしてから再実行してください", 429, True)
            queued = QueuedRequest(asyncio.Event(), time.monotonic())
            self.queued_requests[request_id] = queued
            acquire_task = asyncio.create_task(self._acquire_generation_slot(request_id))
            cancel_task = asyncio.create_task(queued.cancel_requested.wait())
            queue_timeout = limits.get("queueTimeoutSeconds", 3600)
            heartbeat = limits.get("streamHeartbeatSeconds", 10)
            try:
                while not acquire_task.done():
                    self._recover_orphaned_generation_slot("queue_wait")
                    remaining = queue_timeout - (time.monotonic() - queued.enqueued_at)
                    if remaining <= 0:
                        raise MLXBarError("QUEUE_TIMEOUT", "生成待ち時間が上限を超えました", 429, True)
                    done, _ = await asyncio.wait(
                        {acquire_task, cancel_task}, timeout=min(heartbeat, remaining),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_task in done:
                        yield {"type": "completed", "finish_reason": "cancelled"}
                        return
                    if acquire_task in done:
                        break
                    yield {"type": "queue", "state": "waiting",
                           "position": self._queue_position(request_id),
                           "waited_seconds": round(time.monotonic() - queued.enqueued_at, 1)}
                await acquire_task
                generation_acquired = True
            finally:
                if cancel_task:
                    cancel_task.cancel()
                    await asyncio.gather(cancel_task, return_exceptions=True)
                if acquire_task and not acquire_task.done():
                    acquire_task.cancel()
                    await asyncio.gather(acquire_task, return_exceptions=True)
                if not generation_acquired:
                    self._release_generation_slot(request_id)
                self.queued_requests.pop(request_id, None)
        else:
            await self._acquire_generation_slot(request_id)
            generation_acquired = True
        engine = "unknown"
        control: ActiveRequest | None = None
        completed_cleanly = False
        try:
            # The entry check ran before this request waited in the queue. An
            # unload, a model switch or a worker crash in between leaves
            # `loaded` empty, and every waiter behind it would otherwise fail
            # on a None attribute -- outside the try that releases the slot.
            if not self.loaded:
                raise MLXBarError("MODEL_NOT_LOADED",
                                  "待機中にモデルが降ろされました。モデルをロードして再実行してください", 409, True)
            engine = self.loaded.get("engine", "unknown")
            control = ActiveRequest(asyncio.Event(), asyncio.Event(), asyncio.current_task(), engine)
            self.active_requests[request_id] = control
            total_timeout = self.settings.data["generation"]["totalTimeoutSeconds"]
            async with asyncio.timeout(total_timeout):
                if engine == "lm-studio":
                    async for event in self._lmstudio_generate(prompt, options):
                        if control.cancel_requested.is_set():
                            yield {"type": "completed", "finish_reason": "cancelled"}
                            return
                        yield event
                    completed_cleanly = True
                    return
                await self._ensure_memory_capacity()
                # The Worker emits heartbeats while a long prompt is being tokenized
                # or prefilling. Total timeout remains the hard safety boundary.
                timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
                transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
                worker_params = {"prompt": prompt, "images": images, **options}
                worker_params["heartbeat_interval_seconds"] = self.settings.data["generation"].get(
                    "streamHeartbeatSeconds", 10)
                worker_params["memory_limit_ratio"] = self.settings.data["generation"].get(
                    "memoryLimitRatio", 0.9)
                if isinstance(prompt, list):
                    worker_params["messages"] = prompt
                idle_timeout = float(self.settings.data["generation"].get("tokenIdleTimeoutSeconds", 60))
                async with httpx.AsyncClient(transport=transport, base_url="http://worker", timeout=timeout) as client:
                    async with client.stream("POST", "/generate", json={"protocol_version": 1, "request_id": request_id,
                                               "method": "generate", "params": worker_params}) as response:
                        response.raise_for_status()
                        async for line in self._lines_with_idle_timeout(response.aiter_lines(), idle_timeout):
                            if line:
                                event = json.loads(line)
                                if event.get("type") == "progress":
                                    self._record_progress(control, event)
                                yield event
                completed_cleanly = True
        except WorkerStalled as exc:
            # No token and no heartbeat for the idle budget: the worker itself
            # is wedged, so only killing the process recovers it.
            if engine != "lm-studio":
                await self._stop_worker()
                self.loaded = None
            raise MLXBarError("WORKER_STALLED",
                              f"Workerが{exc.seconds:.0f}秒間無応答になったため再起動しました", 504, True) from exc
        except TimeoutError as exc:
            # Merely a long generation. Stop this request and keep the model
            # resident -- reloading a 27B costs minutes and helps nobody. The
            # finalizer sends the cancel, so this branch must not duplicate it.
            raise MLXBarError("GENERATION_TIMEOUT",
                              f"生成が安全上限の{total_timeout}秒を超えたため停止しました", 504, True) from exc
        except httpx.HTTPError as exc:
            if engine != "lm-studio":
                await self._stop_worker()
                self.loaded = None
            raise MLXBarError("WORKER_CRASHED", f"モデルWorkerとの通信が切断されました: {exc}", 503, True) from exc
        finally:
            if control is not None:
                control.done.set()
            self.active_requests.pop(request_id, None)
            if generation_acquired:
                self._release_generation_slot(request_id)
            if not completed_cleanly and engine not in {"unknown", "lm-studio"}:
                # The client went away mid-stream. Tell the worker so it stops
                # at the next token instead of finishing a reply nobody reads.
                self._notify_worker_cancelled(request_id)

    @staticmethod
    def _record_progress(control: ActiveRequest | None, event: dict) -> None:
        """Keep the newest live rate so status can show it while generating."""
        if control is None:
            return
        tokens = event.get("generated_tokens")
        if isinstance(tokens, int) and tokens >= 0:
            control.generated_tokens = tokens
        rate = event.get("generation_tps")
        control.generation_tps = float(rate) if isinstance(rate, (int, float)) and rate > 0 else None
        control.progress_at = time.monotonic()

    def _live_generation(self) -> dict:
        """Current tokens-per-second, or empty when there is nothing to report.

        Reached from `status()`, which the menu bar polls every second, so this
        reports nothing rather than raising if a request has no rate yet.
        """
        now = time.monotonic()
        for control in list(self.active_requests.values()):
            rate = getattr(control, "generation_tps", None)
            stamp = getattr(control, "progress_at", 0.0) or 0.0
            if rate and now - stamp <= PROGRESS_FRESHNESS_SECONDS:
                return {"generationTokensPerSecond": round(float(rate), 1),
                        "generatedTokens": int(getattr(control, "generated_tokens", 0) or 0)}
        return {}

    def _notify_worker_cancelled(self, request_id: str) -> None:
        """Best-effort cancel RPC for a stream the client abandoned.

        Fire-and-forget: this runs inside a finalizer that may itself be
        unwinding a cancellation, so it must not await, and a worker that is
        already gone is not an error worth surfacing."""
        if not self.socket_path:
            return

        async def notify() -> None:
            with contextlib.suppress(Exception):
                await self._call("cancel", {"request_id": request_id}, timeout=5)

        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(notify())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _acquire_generation_slot(self, request_id: str) -> None:
        await self.generation_lock.acquire()
        # Ownership is assigned before another await can expose the lock.
        self.generation_owner = request_id

    def _release_generation_slot(self, request_id: str) -> bool:
        if self.generation_owner != request_id:
            return False
        self.generation_owner = None
        if self.generation_lock.locked():
            self.generation_lock.release()
        return True

    def _recover_orphaned_generation_slot(self, source: str) -> bool:
        """Release a slot whose owner vanished after an interrupted stream."""
        if not self.generation_lock.locked():
            if (self.generation_owner is not None
                    and self.generation_owner not in self.active_requests
                    and self.generation_owner not in self.queued_requests):
                self.generation_owner = None
            return False
        owner = self.generation_owner
        if owner in self.active_requests or owner in self.queued_requests:
            return False
        # Releasing while an unowned active request exists could allow two MLX
        # generations to overlap. Preserve serialization in that inconsistent
        # case and let the active request's normal finalizer finish first.
        if self.active_requests:
            return False
        self.generation_owner = None
        self.generation_lock.release()
        self.generation_lock_recoveries += 1
        LOGGER.error("Recovered orphaned generation lock source=%s", source)
        return True

    async def _lmstudio_generate(self, prompt, options: dict):
        base = self.settings.data["models"]["lmStudio"]["baseUrl"].rstrip("/")
        payload = {"model": self.loaded.get("provider_instance_id") or self.loaded.get("provider_key") or self.loaded["name"],
                   "messages": prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}], "stream": True,
                   "temperature": options["temperature"], "top_p": options["top_p"],
                   "max_tokens": options.get("max_tokens", 512)}
        for key in ("frequency_penalty", "presence_penalty", "seed", "stop"):
            if key in options:
                payload[key] = options[key]
        if options.get("tools"):
            payload["tools"] = options["tools"]
        if options.get("tool_choice") is not None:
            payload["tool_choice"] = options["tool_choice"]
        token = self.settings.lm_studio_token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                async with client.stream("POST", base + "/v1/chat/completions", json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        message = self._lmstudio_error_message(body, response.status_code)
                        yield {"type": "error", "code": "LMSTUDIO_REQUEST_FAILED",
                               "message": message, "retryable": response.status_code >= 500}
                        return
                    heartbeat = self.settings.data["generation"].get("streamHeartbeatSeconds", 10)
                    async for line in self._lines_with_heartbeats(response.aiter_lines(), heartbeat):
                        if line is None:
                            yield {"type": "heartbeat", "phase": "upstream"}
                            continue
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        chunk = json.loads(line[6:])
                        if chunk.get("usage"):
                            yield {"type": "usage", **chunk["usage"]}
                        choice = chunk.get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        text = delta.get("content")
                        if text:
                            yield {"type": "delta", "text": text}
                        if delta.get("tool_calls"):
                            yield {"type": "tool_call_delta", "calls": delta["tool_calls"]}
                        if choice.get("finish_reason"):
                            yield {"type": "completed", "finish_reason": choice["finish_reason"]}
                            return
            except Exception as exc:
                yield {"type": "error", "code": "LMSTUDIO_UNAVAILABLE", "message": str(exc), "retryable": True}
                return
        yield {"type": "completed", "finish_reason": "stop"}

    @staticmethod
    async def _lines_with_idle_timeout(lines, timeout: float):
        """Yield worker lines, raising WorkerStalled after total silence.

        The worker heartbeats every few seconds even while prefilling, so a gap
        this long means the worker stopped responding rather than that the
        model is slow.
        """
        iterator = lines.__aiter__()
        while True:
            pending = asyncio.create_task(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=timeout if timeout > 0 else None)
            if not done:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                raise WorkerStalled(timeout)
            try:
                yield await pending
            except StopAsyncIteration:
                return

    @staticmethod
    async def _lines_with_heartbeats(lines, interval: float = 10.0):
        iterator = lines.__aiter__()
        while True:
            pending = asyncio.create_task(iterator.__anext__())
            while not pending.done():
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if done:
                    break
                yield None
            try:
                yield await pending
            except StopAsyncIteration:
                return

    async def _load_lmstudio(self, model: dict, timeout: float) -> dict:
        base = self.settings.data["models"]["lmStudio"]["baseUrl"].rstrip("/")
        token = self.settings.lm_studio_token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        key = model.get("provider_key") or model.get("name")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(base + "/api/v1/models/load", json={"model": key}, headers=headers)
        except httpx.HTTPError as exc:
            raise MLXBarError("LMSTUDIO_UNAVAILABLE", f"LM Studioへ接続できません: {exc}", 503, True) from exc
        if response.status_code >= 400:
            raise MLXBarError("LMSTUDIO_LOAD_FAILED",
                              self._lmstudio_error_message(response.content, response.status_code),
                              409, response.status_code >= 500)
        try:
            result = response.json()
        except ValueError as exc:
            raise MLXBarError("LMSTUDIO_LOAD_FAILED", "LM Studioから不正な応答が返されました", 502, True) from exc
        if result.get("status") != "loaded" or not result.get("instance_id"):
            raise MLXBarError("LMSTUDIO_LOAD_FAILED", "LM Studioでモデルのロードを確認できませんでした", 502, True)
        return result

    @staticmethod
    def _lmstudio_error_message(body: bytes, status: int) -> str:
        try:
            payload = json.loads(body)
            detail = payload.get("error", payload)
            if isinstance(detail, dict) and detail.get("message"):
                return f"LM Studio: {detail['message']}"
        except (ValueError, TypeError):
            pass
        return f"LM Studioでエラーが発生しました（HTTP {status}）"

    async def cancel(self, request_id: str) -> dict:
        queued = self.queued_requests.get(request_id)
        if queued:
            queued.cancel_requested.set()
            return {"cancelled": True, "forced": False, "queued": True,
                    "message": "生成待ちをキャンセルしました"}
        control = self.active_requests.get(request_id)
        if not control:
            return {"cancelled": False, "forced": False, "message": "対象の生成は実行されていません"}
        control.cancel_requested.set()
        grace = self.settings.data["generation"]["cancelGraceSeconds"]
        if control.engine != "lm-studio":
            try:
                await asyncio.wait_for(self._call("cancel", {"request_id": request_id}), timeout=grace)
            except Exception:
                await self._stop_worker()
                self.loaded = None
                return {"cancelled": True, "forced": True, "message": "Workerを強制終了しました"}
        try:
            await asyncio.wait_for(control.done.wait(), timeout=grace)
            return {"cancelled": True, "forced": False, "message": "生成を停止しました"}
        except asyncio.TimeoutError:
            if control.engine == "lm-studio":
                if control.task and not control.task.done():
                    control.task.cancel()
            else:
                await self._stop_worker()
                self.loaded = None
            try:
                await asyncio.wait_for(control.done.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            return {"cancelled": True, "forced": True,
                    "message": "応答がなかったため生成処理を強制終了しました"}

    def raise_if_queue_full(self) -> None:
        self._recover_orphaned_generation_slot("capacity_check")
        maximum = self.settings.data["generation"].get("maxQueuedRequests", 16)
        if ((self.generation_lock.locked() or self.queued_requests)
                and len(self.queued_requests) >= maximum):
            raise MLXBarError("QUEUE_FULL", "生成待ちが上限に達しています。しばらくしてから再実行してください", 429, True)

    async def cancel_all(self) -> dict:
        queued_count = len(self.queued_requests)
        for queued in list(self.queued_requests.values()):
            queued.cancel_requested.set()
        active_ids = list(self.active_requests)
        results = [await self.cancel(request_id) for request_id in active_ids]
        return {"cancelled": bool(queued_count or active_ids), "queuedCancelled": queued_count,
                "activeCancelled": len(active_ids),
                "forced": any(result.get("forced", False) for result in results),
                "message": (f"実行中{len(active_ids)}件、待機中{queued_count}件へ停止を要求しました"
                            if queued_count or active_ids else "停止対象の生成はありません")}

    def _validate_generation(self, prompt, images, options, image_root: Path | None = None):
        limits = self.settings.data["generation"]
        if not isinstance(prompt, (str, list)):
            raise MLXBarError("INVALID_REQUEST", "promptは文字列またはmessages配列で指定してください", 422)
        prompt_size = len(prompt) if isinstance(prompt, str) else len(json.dumps(prompt, ensure_ascii=False))
        prompt_limit = self.effective_max_prompt_characters()
        if prompt_size > prompt_limit:
            raise MLXBarError("INPUT_TOO_LARGE", f"入力は{prompt_limit}文字以内にしてください", 413)
        if not isinstance(images, list) or not all(isinstance(item, str) for item in images):
            raise MLXBarError("INVALID_REQUEST", "imagesはパスまたはURLの配列で指定してください", 422)
        if len(images) > limits["maxImages"]:
            raise MLXBarError("INPUT_TOO_LARGE", f"画像は最大{limits['maxImages']}件です", 413)
        resolved_root = image_root.resolve() if image_root else None
        for image in images:
            if resolved_root is not None:
                # Callers that pass a root are relaying untrusted references
                # (the public OpenAI API). Only files the coordinator itself
                # materialised inside that root may reach a worker, so a URL or
                # an arbitrary path can never be fetched or read on their behalf.
                try:
                    candidate = Path(image).resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise MLXBarError("INVALID_REQUEST", "画像を参照できません", 422) from exc
                if not candidate.is_relative_to(resolved_root) or not candidate.is_file():
                    raise MLXBarError("INVALID_REQUEST", "画像の参照先が許可されていません", 422)
                path = candidate
            else:
                path = Path(image)
                if not path.exists():
                    continue
            if not path.is_file() or path.stat().st_size > limits["maxImageBytes"]:
                raise MLXBarError("INPUT_TOO_LARGE", f"画像は1件{limits['maxImageBytes'] // 1_048_576}MB以内にしてください", 413)
        defaults = limits
        try:
            max_tokens = int(options.get("max_tokens", 512))
            temperature = float(options.get("temperature", defaults.get("defaultTemperature", 0.7)))
            top_p = float(options.get("top_p", defaults.get("defaultTopP", 1.0)))
            repetition_penalty = float(options.get(
                "repetition_penalty", defaults.get("defaultRepetitionPenalty", 1.0)))
            repetition_context_size = int(options.get(
                "repetition_context_size", defaults.get("repetitionContextSize", 20)))
            presence_penalty = float(options.get("presence_penalty", 0.0))
            frequency_penalty = float(options.get("frequency_penalty", 0.0))
        except (TypeError, ValueError) as exc:
            raise MLXBarError("INVALID_REQUEST", "生成パラメータの値が不正です", 422) from exc
        if max_tokens < 1:
            raise MLXBarError("INVALID_REQUEST", "max_tokensは1以上で指定してください", 422)
        effective_limit = self.effective_max_tokens()
        max_tokens = min(max_tokens, effective_limit)
        if not 0 <= temperature <= 2:
            raise MLXBarError("INVALID_REQUEST", "temperatureは0〜2で指定してください", 422)
        if not 0 <= top_p <= 1:
            raise MLXBarError("INVALID_REQUEST", "top_pは0〜1で指定してください", 422)
        if not 0.01 <= repetition_penalty <= 2:
            raise MLXBarError("INVALID_REQUEST", "repetition_penaltyは0.01〜2で指定してください", 422)
        if not 1 <= repetition_context_size <= 32768:
            raise MLXBarError("INVALID_REQUEST", "repetition_context_sizeは1〜32768で指定してください", 422)
        if not -2 <= presence_penalty <= 2 or not -2 <= frequency_penalty <= 2:
            raise MLXBarError("INVALID_REQUEST", "presence_penaltyとfrequency_penaltyは-2〜2で指定してください", 422)
        return prompt, images, {**options, "max_tokens": max_tokens, "temperature": temperature,
                                "top_p": top_p, "repetition_penalty": repetition_penalty,
                                "repetition_context_size": repetition_context_size,
                                "presence_penalty": presence_penalty,
                                "frequency_penalty": frequency_penalty}

    def effective_max_tokens(self) -> int:
        configured = int(self.settings.data["generation"]["maxTokens"])
        capabilities = (self.loaded or {}).get("capabilities") or {}
        model_limit = capabilities.get("modelMaxTokens")
        if isinstance(model_limit, int) and model_limit > 0:
            return min(configured, model_limit)
        return configured

    def effective_max_prompt_characters(self) -> int:
        configured = int(self.settings.data["generation"]["maxPromptCharacters"])
        capabilities = (self.loaded or {}).get("capabilities") or {}
        model_limit = capabilities.get("modelMaxTokens")
        if isinstance(model_limit, int) and model_limit > 0:
            # Character count is only a preflight safety check. Tokenizers vary,
            # so leave the exact context calculation to the loaded runtime.
            return max(configured, min(model_limit * 4, 10_000_000))
        return configured

    @staticmethod
    def _detect_model_max_tokens(path: str | None) -> int | None:
        if not path:
            return None
        root = Path(path)
        candidates: list[int] = []
        for filename in ("config.json", "tokenizer_config.json", "generation_config.json"):
            file = root / filename
            if not file.is_file():
                continue
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            sections = [data]
            for key in ("text_config", "llm_config", "language_config"):
                if isinstance(data.get(key), dict):
                    sections.append(data[key])
            for section in sections:
                for key in ("max_position_embeddings", "max_sequence_length", "max_seq_len",
                            "seq_length", "context_length", "model_max_length", "n_positions"):
                    value = section.get(key)
                    if isinstance(value, int) and 128 <= value <= 10_000_000:
                        candidates.append(value)
        return max(candidates) if candidates else None

    @staticmethod
    def memory_pressure_reason(memory: dict, limit: float) -> str | None:
        """Short reason why generating now is unsafe, or None.

        Mirrors the worker's in-generation watchdog. A ratio against *total*
        RAM cannot see the rest of the machine, so free memory and macOS's own
        pressure verdict both count, and the process's resident size stands in
        for everything MLX's own counters miss.
        """
        if int(memory.get("pressure_level", 0)) >= 4:
            return "OSがメモリ逼迫を報告しています"
        physical = int(memory.get("physical_memory_bytes", 0))
        if physical <= 0:
            return None
        used = max(int(memory.get("active_bytes", 0)) + int(memory.get("cache_bytes", 0)),
                   int(memory.get("process_rss_bytes", 0)))
        if used / physical >= limit:
            return f"MLX使用量が物理メモリの{used / physical:.0%}に達しています"
        available = int(memory.get("available_bytes", 0))
        if available > 0 and available < physical * (1 - limit):
            return f"空きメモリが{available / physical:.0%}まで低下しています"
        return None

    async def _ensure_memory_capacity(self) -> None:
        try:
            limit = float(self.settings.data["generation"]["memoryLimitRatio"])
            result = await self._call("memory", {}, timeout=5)
            reason = self.memory_pressure_reason(result.get("memory", {}), limit)
            if reason:
                # A retained ZCode prefix is disposable. Drop it once before
                # rejecting the request so caching cannot turn memory safety
                # into a persistent failure mode.
                await self._call("clear_prompt_cache", {}, timeout=5)
                result = await self._call("memory", {}, timeout=5)
                reason = self.memory_pressure_reason(result.get("memory", {}), limit)
                if reason:
                    raise MLXBarError("MEMORY_PRESSURE",
                                      f"メモリ安全上限に達しているため生成を中止しました（{reason}）", 503, True)
        except MLXBarError:
            raise
        except Exception:
            # 古いランタイムがmemory RPCに未対応でも生成互換性を維持する。
            return

    async def shutdown(self) -> None:
        """Stop the worker process and clear the orphan manifest."""
        await self._stop_worker()
        self.loaded = None
        self.loading = None

    async def probe_runtime(self, engine: str) -> dict:
        if self.loaded:
            raise MLXBarError("ENGINE_BUSY", "別のモデルがロード中です", 409, True)
        await self._start_worker(engine)
        try:
            return await self._call("health", {}, timeout=10)
        finally:
            await self._stop_worker()

    async def wait_until_idle(self, timeout: float = 30) -> bool:
        try:
            async with asyncio.timeout(timeout):
                while self.active_requests or self.queued_requests or self.generation_lock.locked():
                    self._recover_orphaned_generation_slot("wait_until_idle")
                    await asyncio.sleep(0.1)
            return True
        except TimeoutError:
            return False

    def begin_maintenance(self, engine: str) -> None:
        self.maintenance_engines.add(engine)

    def end_maintenance(self, engine: str) -> None:
        self.maintenance_engines.discard(engine)

    def _forget_dead_worker(self) -> None:
        """Drop `loaded` when the worker process died with no request in flight.

        A worker killed while idle (memory pressure, Activity Monitor) leaves
        nothing to notice it until the next generate call, so status would keep
        reporting a ready model that no longer exists.
        """
        if not self.loaded or self.loaded.get("engine") == "lm-studio":
            return
        if self.process is not None and self.process.returncode is None:
            return
        if self.active_requests or self.queued_requests or self.loading:
            return
        self.loaded = None

    def status(self) -> dict:
        self._recover_orphaned_generation_slot("status")
        self._forget_dead_worker()
        loaded = None if not self.loaded else {
            **self.loaded,
            "effectiveMaxTokens": self.effective_max_tokens(),
            "effectiveMaxPromptCharacters": self.effective_max_prompt_characters(),
        }
        return {"loadedModel": loaded, "worker": self.engine,
                "loadingModel": self.loading,
                "workerRunning": bool(self.process and self.process.returncode is None),
                "activeRequestCount": len(self.active_requests),
                "queuedRequestCount": len(self.queued_requests),
                "oldestQueuedSeconds": self._oldest_queued_seconds(),
                "generationLockState": self._generation_lock_state(),
                "generationLockRecoveries": self.generation_lock_recoveries,
                "maintenanceEngines": sorted(self.maintenance_engines),
                **self._live_generation()}

    def _generation_lock_state(self) -> str:
        if not self.generation_lock.locked():
            return "inconsistent" if self.active_requests else "idle"
        if self.generation_owner in self.active_requests:
            return "active"
        if self.generation_owner in self.queued_requests:
            return "handoff"
        return "inconsistent"

    def _queue_position(self, request_id: str) -> int:
        try:
            return list(self.queued_requests).index(request_id) + 1
        except ValueError:
            return 0

    def _oldest_queued_seconds(self) -> int:
        if not self.queued_requests:
            return 0
        return round(time.monotonic() - min(item.enqueued_at for item in self.queued_requests.values()))
