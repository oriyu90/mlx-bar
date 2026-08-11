from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..errors import MLXBarError


@dataclass
class ActiveRequest:
    done: asyncio.Event
    cancel_requested: asyncio.Event
    task: asyncio.Task | None
    engine: str


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

    async def _start_worker(self, engine: str) -> None:
        if self.process and self.process.returncode is None and self.engine == engine:
            return
        await self._stop_worker()
        socket_dir = Path(tempfile.gettempdir()) / f"mlxbar-{os.getuid()}"
        socket_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_dir, 0o700)
        self.socket_path = socket_dir / f"{engine}-{uuid.uuid4().hex[:8]}.sock"
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([*self._worker_import_paths(), env.get("PYTHONPATH", "")])
        module = "mlx_lm_worker.adapter" if engine == "mlx-lm" else "mlx_vlm_worker.adapter"
        python = self._runtime_python(engine)
        self.process = await asyncio.create_subprocess_exec(
            str(python), "-m", module, "--socket", str(self.socket_path), env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self.engine = engine
        for _ in range(50):
            if self.process.returncode is not None:
                detail = (await self.process.stderr.read()).decode(errors="replace")[-1000:]
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
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        if self.socket_path:
            self.socket_path.unlink(missing_ok=True)
        self.socket_path = None
        self.engine = None

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
            await asyncio.wait_for(self._call("unload", {}, timeout=5), timeout=5)
        except Exception:
            await self._stop_worker()
        self.loaded = None
        return {"state": "unloaded"}

    async def generate(self, prompt, images: list[str], options: dict, request_id: str | None = None):
        if not self.loaded:
            raise MLXBarError("MODEL_NOT_LOADED", "モデルがロードされていません", 409)
        if self.loaded.get("engine") in self.maintenance_engines:
            raise MLXBarError("ENGINE_BUSY", "ランタイム切替中のため新しい生成を開始できません", 409, True)
        prompt, images, options = self._validate_generation(prompt, images, options)
        request_id = request_id or str(uuid.uuid4())
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
            acquire_task = asyncio.create_task(self.generation_lock.acquire())
            cancel_task = asyncio.create_task(queued.cancel_requested.wait())
            queue_timeout = limits.get("queueTimeoutSeconds", 3600)
            heartbeat = limits.get("streamHeartbeatSeconds", 10)
            try:
                while not acquire_task.done():
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
                self.queued_requests.pop(request_id, None)
                if cancel_task:
                    cancel_task.cancel()
                    await asyncio.gather(cancel_task, return_exceptions=True)
                if acquire_task and not acquire_task.done():
                    acquire_task.cancel()
                    await asyncio.gather(acquire_task, return_exceptions=True)
                elif (acquire_task and acquire_task.done() and not generation_acquired
                      and not acquire_task.cancelled() and acquire_task.exception() is None):
                    self.generation_lock.release()
        else:
            await self.generation_lock.acquire()
            generation_acquired = True
        engine = self.loaded.get("engine", "unknown")
        control = ActiveRequest(asyncio.Event(), asyncio.Event(), asyncio.current_task(), engine)
        self.active_requests[request_id] = control
        total_timeout = self.settings.data["generation"]["totalTimeoutSeconds"]
        try:
            async with asyncio.timeout(total_timeout):
                if engine == "lm-studio":
                    async for event in self._lmstudio_generate(prompt, options):
                        if control.cancel_requested.is_set():
                            yield {"type": "completed", "finish_reason": "cancelled"}
                            return
                        yield event
                    return
                await self._ensure_memory_capacity()
                # The Worker emits heartbeats while a long prompt is being tokenized
                # or prefilling. Total timeout remains the hard safety boundary.
                timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
                transport = httpx.AsyncHTTPTransport(uds=str(self.socket_path))
                worker_params = {"prompt": prompt, "images": images, **options}
                worker_params["heartbeat_interval_seconds"] = self.settings.data["generation"].get(
                    "streamHeartbeatSeconds", 10)
                if isinstance(prompt, list):
                    worker_params["messages"] = prompt
                async with httpx.AsyncClient(transport=transport, base_url="http://worker", timeout=timeout) as client:
                    async with client.stream("POST", "/generate", json={"protocol_version": 1, "request_id": request_id,
                                               "method": "generate", "params": worker_params}) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if line:
                                yield json.loads(line)
        except (httpx.TimeoutException, TimeoutError) as exc:
            if engine != "lm-studio":
                await self._stop_worker()
                self.loaded = None
            raise MLXBarError("GENERATION_TIMEOUT",
                              f"生成が安全上限の{total_timeout}秒を超えたため停止しました", 504, True) from exc
        except httpx.HTTPError as exc:
            if engine != "lm-studio":
                await self._stop_worker()
                self.loaded = None
            raise MLXBarError("WORKER_CRASHED", f"モデルWorkerとの通信が切断されました: {exc}", 503, True) from exc
        finally:
            control.done.set()
            self.active_requests.pop(request_id, None)
            if generation_acquired and self.generation_lock.locked():
                self.generation_lock.release()

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

    def _validate_generation(self, prompt, images, options):
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
        for image in images:
            path = Path(image)
            if path.exists() and (not path.is_file() or path.stat().st_size > limits["maxImageBytes"]):
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

    async def _ensure_memory_capacity(self) -> None:
        try:
            result = await self._call("memory", {}, timeout=5)
            memory = result.get("memory", {})
            used = int(memory.get("active_bytes", 0)) + int(memory.get("cache_bytes", 0))
            physical = int(memory.get("physical_memory_bytes", 0))
            limit = float(self.settings.data["generation"]["memoryLimitRatio"])
            if physical > 0 and used / physical >= limit:
                raise MLXBarError("MEMORY_PRESSURE", "MLXメモリ使用量が安全上限に達しているため生成を中止しました", 503, True)
        except MLXBarError:
            raise
        except Exception:
            # 古いランタイムがmemory RPCに未対応でも生成互換性を維持する。
            return

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
                while self.active_requests or self.queued_requests:
                    await asyncio.sleep(0.1)
            return True
        except TimeoutError:
            return False

    def begin_maintenance(self, engine: str) -> None:
        self.maintenance_engines.add(engine)

    def end_maintenance(self, engine: str) -> None:
        self.maintenance_engines.discard(engine)

    def status(self) -> dict:
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
                "maintenanceEngines": sorted(self.maintenance_engines)}

    def _queue_position(self, request_id: str) -> int:
        try:
            return list(self.queued_requests).index(request_id) + 1
        except ValueError:
            return 0

    def _oldest_queued_seconds(self) -> int:
        if not self.queued_requests:
            return 0
        return round(time.monotonic() - min(item.enqueued_at for item in self.queued_requests.values()))
