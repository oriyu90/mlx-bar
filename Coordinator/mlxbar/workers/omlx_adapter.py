from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..errors import MLXBarError

LOGGER = logging.getLogger(__name__)


@dataclass
class OMLXModelInfo:
    """Information about an oMLX model."""
    id: str
    name: str
    loaded: bool = False
    memory_bytes: int = 0
    context_length: int = 0
    mtp_enabled: bool = False
    ple_offload: bool = False
    engine: str = "omlx"
    provider_key: str | None = None


@dataclass
class OMLXStatus:
    """oMLX server status."""
    connected: bool = False
    version: str | None = None
    error: str | None = None
    models: list[OMLXModelInfo] = field(default_factory=list)
    total_memory_bytes: int = 0
    server_url: str = ""


class OMLXAdapter:
    """Adapter for communicating with oMLX server via its admin API."""

    def __init__(self, server_url: str, api_key: str | None = None):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._status_cache: OMLXStatus | None = None
        self._cache_at: float = 0.0
        self._cache_ttl = 5.0  # 5 second cache for status
        # v2.10.0: connection identity so a changed URL/token in settings
        # rebuilds the client instead of living with the old one until the
        # coordinator restarts.
        self._identity = (self.server_url, self.api_key)

    def _get_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.server_url,
                headers=self._get_headers(),
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def test_connection(self) -> OMLXStatus:
        """Test connection to oMLX server and return status.

        v2.10.0: the health fetch also attaches the model list and the total
        memory so `/omlx/status`, the oMLX tab's routing list and the
        memory-budget charging actually work (v2.9.0 always returned zero
        models / 0 bytes here because the status came from the probe alone).
        A failed model fetch never fails the connection itself."""
        client = await self._get_client()
        try:
            # Try admin API first for version info
            response = await client.get("/admin/api/health", timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                version = data.get("version") or data.get("omlx_version")
                status = OMLXStatus(
                    connected=True,
                    version=version,
                    server_url=self.server_url,
                )
            else:
                # Fallback to standard OpenAI-compatible health check
                response = await client.get("/v1/models", timeout=10.0)
                if response.status_code == 200:
                    status = OMLXStatus(
                        connected=True,
                        version=None,
                        server_url=self.server_url,
                    )
                else:
                    status = OMLXStatus(
                        connected=False,
                        error=f"HTTP {response.status_code}",
                        server_url=self.server_url,
                    )
        except httpx.ConnectError as exc:
            status = OMLXStatus(
                connected=False,
                error=f"Connection failed: {exc}",
                server_url=self.server_url,
            )
        except httpx.TimeoutException as exc:
            status = OMLXStatus(
                connected=False,
                error=f"Connection timeout: {exc}",
                server_url=self.server_url,
            )
        except Exception as exc:
            status = OMLXStatus(
                connected=False,
                error=f"Unexpected error: {exc}",
                server_url=self.server_url,
            )
        if status.connected:
            # Model fetch failure never fails the connection itself.
            with contextlib.suppress(Exception):
                status.models = await self.list_models()
                status.total_memory_bytes = sum(m.memory_bytes for m in status.models)
        self._status_cache = status
        self._cache_at = time.monotonic()
        return status

    async def get_status(self, force_refresh: bool = False) -> OMLXStatus:
        """Get cached or fresh oMLX status."""
        now = time.monotonic()
        if not force_refresh and self._status_cache and (now - self._cache_at) < self._cache_ttl:
            return self._status_cache
        return await self.test_connection()

    async def list_models(self) -> list[OMLXModelInfo]:
        """List models from oMLX server."""
        client = await self._get_client()
        try:
            # Try admin API first for detailed model info
            response = await client.get("/admin/api/models", timeout=15.0)
            if response.status_code == 200:
                data = response.json()
                models = []
                for item in data.get("models", []):
                    models.append(OMLXModelInfo(
                        id=item.get("id", ""),
                        name=item.get("name", item.get("id", "")),
                        loaded=item.get("loaded", False),
                        memory_bytes=int(item.get("memory_bytes", item.get("vram_bytes", 0) or 0)),
                        context_length=int(item.get("context_length", item.get("max_context_length", 0) or 0)),
                        mtp_enabled=bool(item.get("mtp_enabled", item.get("mtp", False))),
                        ple_offload=bool(item.get("ple_offload", item.get("ple", False))),
                        provider_key=item.get("id"),
                    ))
                return models
        except Exception as exc:
            LOGGER.debug("Admin API models failed: %s, trying /v1/models", exc)

        # Fallback to OpenAI-compatible /v1/models
        try:
            response = await client.get("/v1/models", timeout=15.0)
            if response.status_code == 200:
                data = response.json()
                models = []
                for item in data.get("data", []):
                    models.append(OMLXModelInfo(
                        id=item.get("id", ""),
                        name=item.get("id", ""),
                        loaded=False,  # Can't determine from /v1/models
                        provider_key=item.get("id"),
                    ))
                return models
        except Exception as exc:
            LOGGER.debug("/v1/models failed: %s", exc)

        return []

    async def load_model(self, model_id: str) -> dict[str, Any]:
        """Load a model in oMLX."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"/v1/models/{model_id}/load",
                json={},
                timeout=300.0,  # Model loading can take a long time
            )
            if response.status_code >= 400:
                error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                raise MLXBarError(
                    "OMLX_LOAD_FAILED",
                    error_data.get("message", f"oMLX returned HTTP {response.status_code}"),
                    response.status_code,
                    response.status_code >= 500,
                )
            return response.json()
        except httpx.TimeoutException as exc:
            raise MLXBarError("OMLX_LOAD_TIMEOUT", f"Model load timed out: {exc}", 504, True) from exc
        except httpx.HTTPError as exc:
            raise MLXBarError("OMLX_CONNECTION_FAILED", f"Failed to connect to oMLX: {exc}", 503, True) from exc

    async def unload_model(self, model_id: str) -> dict[str, Any]:
        """Unload a model from oMLX."""
        client = await self._get_client()
        try:
            response = await client.post(
                f"/v1/models/{model_id}/unload",
                json={},
                timeout=30.0,
            )
            if response.status_code >= 400:
                error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                raise MLXBarError(
                    "OMLX_UNLOAD_FAILED",
                    error_data.get("message", f"oMLX returned HTTP {response.status_code}"),
                    response.status_code,
                    response.status_code >= 500,
                )
            return response.json()
        except httpx.TimeoutException as exc:
            raise MLXBarError("OMLX_UNLOAD_TIMEOUT", f"Model unload timed out: {exc}", 504, True) from exc
        except httpx.HTTPError as exc:
            raise MLXBarError("OMLX_CONNECTION_FAILED", f"Failed to connect to oMLX: {exc}", 503, True) from exc

    async def generate(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        request_id: str,
    ):
        """Generate completion via oMLX (OpenAI-compatible streaming)."""
        client = await self._get_client()
        payload = {
            "model": model_id,
            "messages": messages,
            "stream": True,
            "temperature": options.get("temperature", 0.7),
            "top_p": options.get("top_p", 1.0),
            "max_tokens": options.get("max_tokens", 512),
        }

        # Add optional parameters
        for key in ("frequency_penalty", "presence_penalty", "seed", "stop"):
            if key in options:
                payload[key] = options[key]

        # Tool calling support
        if options.get("tools"):
            payload["tools"] = options["tools"]
        if options.get("tool_choice") is not None:
            payload["tool_choice"] = options["tool_choice"]

        try:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json=payload,
                timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    try:
                        error_data = json.loads(body)
                        message = error_data.get("error", {}).get("message", f"HTTP {response.status_code}")
                    except Exception:
                        message = f"oMLX HTTP {response.status_code}"
                    yield {"type": "error", "code": "OMLX_REQUEST_FAILED", "message": message, "retryable": response.status_code >= 500}
                    return

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        break
                    try:
                        chunk = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    if chunk.get("usage"):
                        yield {"type": "usage", **chunk["usage"]}

                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})

                    # Text content
                    text = delta.get("content")
                    if text:
                        yield {"type": "delta", "text": text}

                    # Tool calls
                    if delta.get("tool_calls"):
                        yield {"type": "tool_call_delta", "calls": delta["tool_calls"]}

                    # Finish reason
                    if choice.get("finish_reason"):
                        yield {"type": "completed", "finish_reason": choice["finish_reason"]}
                        return

        except httpx.TimeoutException as exc:
            yield {"type": "error", "code": "OMLX_TIMEOUT", "message": f"Request timed out: {exc}", "retryable": True}
            return
        except httpx.HTTPError as exc:
            yield {"type": "error", "code": "OMLX_CONNECTION_FAILED", "message": f"Connection failed: {exc}", "retryable": True}
            return
        except Exception as exc:
            yield {"type": "error", "code": "OMLX_ERROR", "message": str(exc), "retryable": False}
            return

    async def cancel_generation(self, request_id: str) -> bool:
        """Cancel a generation - oMLX doesn't have a direct cancel API, so we rely on client-side cancellation."""
        # oMLX doesn't expose a cancellation endpoint. The client closes the stream.
        # We can't do server-side cancellation, so this is a no-op that returns True
        # to indicate the request was acknowledged.
        return True

    async def get_model_memory(self, model_id: str) -> int:
        """Get memory usage for a specific model."""
        status = await self.get_status(force_refresh=True)
        for model in status.models:
            if model.id == model_id:
                return model.memory_bytes
        return 0

    async def get_total_memory(self) -> int:
        """Get total memory used by oMLX."""
        status = await self.get_status(force_refresh=True)
        return status.total_memory_bytes


class OMLXProvider:
    """High-level provider interface matching the worker supervisor pattern."""

    def __init__(self, settings, root_path):
        self.settings = settings
        self.root = root_path
        self.adapter: OMLXAdapter | None = None
        self._loaded_models: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._cached_status: dict[str, Any] = {"connected": False, "models": [], "totalMemoryBytes": 0}
        self._status_update_task: asyncio.Task | None = None
        self._pending_closes: list[asyncio.Task] = []

    def _get_omlx_config(self) -> dict[str, Any]:
        return self.settings.data.get("models", {}).get("omlx", {})

    def _is_model_routed_to_omlx(self, model_id: str) -> bool:
        routing = self._get_omlx_config().get("modelRouting", {})
        return isinstance(routing, dict) and bool(routing.get(model_id, False))

    def _connection_credentials(self) -> tuple[str, str | None]:
        config = self._get_omlx_config()
        server_url = str(config.get("serverUrl", "http://127.0.0.1:8000") or "http://127.0.0.1:8000")
        api_key = config.get("apiKey")
        # Token lives in a separate 0600 file; prefer the dedicated token
        # source and always re-read it so a token update is live immediately.
        token = None
        if hasattr(self.settings, "omlx_token"):
            token = self.settings.omlx_token
        if not token and isinstance(api_key, str):
            token = api_key
        return server_url, token

    def _ensure_adapter(self) -> OMLXAdapter:
        # v2.10.0: re-derive the adapter from the *current* settings every
        # time, and rebuild it when the URL or token changed. v2.9.0 latched
        # the first server URL forever, so changing it in the settings UI
        # never took effect until the coordinator restarted.
        server_url, token = self._connection_credentials()
        identity = (server_url, token)
        adapter = self.adapter
        if adapter is not None and adapter._identity == identity:
            return adapter
        if adapter is not None:
            # Retire the old client in the background so switching URLs never
            # blocks the caller; the list is bounded so a churn of edits
            # cannot accumulate tasks forever.
            old = adapter

            async def _retire() -> None:
                with contextlib.suppress(Exception):
                    await old.close()

            self._pending_closes.append(asyncio.create_task(_retire()))
            self._pending_closes = self._pending_closes[-8:]
        self.adapter = OMLXAdapter(server_url, token)
        return self.adapter

    async def connect(self) -> OMLXStatus:
        """Test and establish connection to oMLX."""
        adapter = self._ensure_adapter()
        return await adapter.test_connection()

    def _model_payload(self, m: OMLXModelInfo) -> dict[str, Any]:
        """v2.10.0: one canonical dict shape for status()/list_models().

        Field names the GUI actually reads: `memoryBytes`/`pleOffload` (the
        v2.9.0 GUI decoders expected `memoryGB`/`pleOffloadEnabled`, which the
        server never emitted, so memory/PLE rows never rendered). `available`
        is the routing-toggle gate: models surfaced by the oMLX server are
        available there; a catalog-only model is not."""
        return {
            "id": m.id,
            "name": m.name,
            "loaded": m.loaded,
            "memoryBytes": m.memory_bytes,
            "contextLength": m.context_length,
            "mtpEnabled": m.mtp_enabled,
            "pleOffload": m.ple_offload,
            "engine": m.engine,
            "providerKey": m.provider_key,
            "available": True,
        }

    async def status(self) -> dict[str, Any]:
        """Get comprehensive oMLX status."""
        adapter = self._ensure_adapter()
        status = await adapter.get_status()
        return {
            "connected": status.connected,
            "version": status.version,
            "error": status.error,
            "serverUrl": status.server_url,
            "models": [self._model_payload(m) for m in status.models],
            "totalMemoryBytes": status.total_memory_bytes,
        }

    async def list_models(self) -> list[dict[str, Any]]:
        """List all oMLX models."""
        adapter = self._ensure_adapter()
        models = await adapter.list_models()
        return [self._model_payload(m) for m in models]

    async def load(self, model: dict[str, Any], engine: str | None = None) -> dict[str, Any]:
        """Load a model via oMLX."""
        if engine and engine != "omlx":
            raise MLXBarError("ENGINE_MISMATCH", f"Expected oMLX engine, got {engine}", 409)

        model_id = str(model.get("id", ""))
        if not model_id:
            raise MLXBarError("MODEL_INCOMPATIBLE", "モデルIDがありません", 409)

        if not self._is_model_routed_to_omlx(model_id):
            raise MLXBarError("MODEL_INCOMPATIBLE", "このモデルはoMLXにルーティングされていません", 409)

        adapter = self._ensure_adapter()
        result = await adapter.load_model(model_id)

        # Report the model's real context window when the oMLX status carries
        # it; fall back to the catalog guess otherwise.
        model_max_tokens = 0
        with contextlib.suppress(Exception):
            for info in (await adapter.get_status()).models:
                if info.id == model_id and info.context_length:
                    model_max_tokens = int(info.context_length)
                    break

        loaded_info = {
            **model,
            "engine": "omlx",
            "state": "loaded",
            "capabilities": {
                "modelMaxTokens": model_max_tokens or model.get("context_length", 32768),
                "toolCalling": True,
                "streaming": True,
            },
            "provider": "omlx",
            "omlxInstanceId": result.get("instance_id"),
        }
        self._loaded_models[model_id] = loaded_info
        return loaded_info

    async def unload(self, model_id: str) -> dict[str, Any]:
        """Unload a model from oMLX."""
        adapter = self._ensure_adapter()
        await adapter.unload_model(model_id)
        self._loaded_models.pop(model_id, None)
        return {"state": "unloaded", "id": model_id}

    async def generate(
        self,
        model_id: str,
        prompt,
        images: list[str],
        options: dict[str, Any],
        request_id: str,
    ):
        """Generate via oMLX."""
        if model_id not in self._loaded_models:
            raise MLXBarError("MODEL_NOT_LOADED", "oMLXモデルがロードされていません", 409)

        adapter = self._ensure_adapter()

        # Convert prompt to messages format
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        else:
            messages = prompt

        async for event in adapter.generate(model_id, messages, options, request_id):
            yield event

    async def cancel(self, request_id: str) -> dict[str, Any]:
        """Cancel a generation."""
        adapter = self._ensure_adapter()
        await adapter.cancel_generation(request_id)
        return {"cancelled": True, "forced": False, "message": "生成をキャンセルしました"}

    async def get_memory_usage(self) -> dict[str, Any]:
        """Get memory usage for oMLX models."""
        adapter = self._ensure_adapter()
        total = await adapter.get_total_memory()
        models = {}
        for model_id in self._loaded_models:
            models[model_id] = await adapter.get_model_memory(model_id)
        return {
            "total_bytes": total,
            "models": models,
        }

    async def close(self) -> None:
        """Close the adapter connection."""
        if self._status_update_task:
            self._status_update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_update_task
        while self._pending_closes:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pending_closes.pop(0)
        if self.adapter:
            await self.adapter.close()
            self.adapter = None

    def start_status_updates(self) -> None:
        """Start background task to update cached status."""
        if self._status_update_task is None or self._status_update_task.done():
            self._status_update_task = asyncio.create_task(self._status_update_loop())

    async def _status_update_loop(self) -> None:
        """Background loop to update cached oMLX status."""
        while True:
            try:
                await asyncio.sleep(5.0)  # Update every 5 seconds
                await self._update_cached_status()
            except asyncio.CancelledError:
                break
            except Exception:
                # Ignore errors in background updates
                pass

    async def _update_cached_status(self) -> None:
        """Update the cached status from oMLX server."""
        try:
            adapter = self._ensure_adapter()
            status = await adapter.get_status()
            self._cached_status = {
                "connected": status.connected,
                "version": status.version,
                "error": status.error,
                "serverUrl": status.server_url,
                "models": [self._model_payload(m) for m in status.models],
                "totalMemoryBytes": status.total_memory_bytes,
            }
        except Exception:
            self._cached_status = {"connected": False, "error": "Failed to fetch status", "models": [], "totalMemoryBytes": 0}

    def get_cached_status(self) -> dict[str, Any]:
        """Get cached oMLX status (synchronous)."""
        return self._cached_status