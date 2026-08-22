from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from mlxbar.api.management import generate as management_generate
from mlxbar.errors import MLXBarError
from mlxbar.main import make_management_app, make_public_app
from mlxbar.settings import SettingsStore
from mlxbar.workers.supervisor import (ActiveRequest, QueuedRequest, WorkerStalled,
                                        WorkerSupervisor)


class ControlledSupervisor(WorkerSupervisor):
    def __init__(self, root: Path, settings):
        super().__init__(root, settings)
        self.stream_started = asyncio.Event()
        self.stream_release = asyncio.Event()
        self.stopped = False
        self.started_prompts = []

    async def _lmstudio_generate(self, prompt: str, options: dict):
        self.started_prompts.append(prompt)
        self.stream_started.set()
        await self.stream_release.wait()
        yield {"type": "delta", "text": "ok"}

    async def _stop_worker(self):
        self.stopped = True
        self.process = None
        self.engine = None


class StressSupervisor(WorkerSupervisor):
    async def _lmstudio_generate(self, prompt: str, options: dict):
        await asyncio.sleep(0.001)
        yield {"type": "delta", "text": str(prompt)}


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_parallel_generations_are_queued_and_run_in_fifo_order(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.settings.data["generation"]["streamHeartbeatSeconds"] = 0.01
        supervisor.settings.data["generation"]["cancelGraceSeconds"] = 0.01
        supervisor.loaded = {"id": "model", "name": "model", "engine": "lm-studio"}
        first = supervisor.generate("first", [], {}, "first")
        first_event = asyncio.create_task(anext(first))
        await supervisor.stream_started.wait()

        second = supervisor.generate("second", [], {}, "second")
        third = supervisor.generate("third", [], {}, "third")
        second_wait = await anext(second)
        third_wait = await anext(third)
        self.assertEqual((second_wait["type"], second_wait["position"]), ("queue", 1))
        self.assertEqual((third_wait["type"], third_wait["position"]), ("queue", 2))
        self.assertEqual(supervisor.status()["queuedRequestCount"], 2)

        supervisor.stream_release.set()
        self.assertEqual((await first_event)["text"], "ok")
        await first.aclose()
        self.assertEqual((await anext(second))["text"], "ok")
        await second.aclose()
        self.assertEqual((await anext(third))["text"], "ok")
        await third.aclose()
        self.assertEqual(supervisor.started_prompts, ["first", "second", "third"])
        self.assertEqual(supervisor.status()["queuedRequestCount"], 0)

    async def test_queued_generation_can_be_cancelled_without_stopping_active_one(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.settings.data["generation"]["streamHeartbeatSeconds"] = 0.01
        supervisor.loaded = {"id": "model", "name": "model", "engine": "lm-studio"}
        first = supervisor.generate("first", [], {}, "first")
        first_event = asyncio.create_task(anext(first))
        await supervisor.stream_started.wait()
        queued = supervisor.generate("queued", [], {}, "queued")
        self.assertEqual((await anext(queued))["type"], "queue")

        result = await supervisor.cancel("queued")
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["queued"])
        self.assertEqual((await anext(queued))["finish_reason"], "cancelled")
        await queued.aclose()
        self.assertEqual(supervisor.status()["activeRequestCount"], 1)
        self.assertEqual(supervisor.status()["queuedRequestCount"], 0)

        supervisor.stream_release.set()
        await first_event
        await first.aclose()

    async def test_disconnected_queued_generation_is_removed(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.settings.data["generation"]["streamHeartbeatSeconds"] = 0.01
        supervisor.loaded = {"id": "model", "name": "model", "engine": "lm-studio"}
        first = supervisor.generate("first", [], {}, "first")
        first_event = asyncio.create_task(anext(first))
        await supervisor.stream_started.wait()
        supervisor.settings.data["generation"]["streamHeartbeatSeconds"] = 10
        queued = supervisor.generate("queued", [], {}, "queued")
        waiting = asyncio.create_task(anext(queued))
        for _ in range(20):
            if supervisor.status()["queuedRequestCount"] == 1:
                break
            await asyncio.sleep(0.001)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await queued.aclose()
        self.assertEqual(supervisor.status()["queuedRequestCount"], 0)
        supervisor.stream_release.set()
        await first_event
        await first.aclose()

    async def test_orphaned_generation_lock_is_recovered_before_next_request(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.loaded = {"id": "model", "name": "model", "engine": "lm-studio"}
        await supervisor.generation_lock.acquire()
        supervisor.generation_owner = "disconnected-request"

        self.assertEqual(supervisor.status()["generationLockState"], "idle")
        self.assertEqual(supervisor.status()["generationLockRecoveries"], 1)

        generation = supervisor.generate("next", [], {}, "next")
        event = asyncio.create_task(anext(generation))
        await supervisor.stream_started.wait()
        supervisor.stream_release.set()
        self.assertEqual((await event)["text"], "ok")
        await generation.aclose()
        self.assertFalse(supervisor.generation_lock.locked())
        self.assertIsNone(supervisor.generation_owner)

    async def test_valid_queue_handoff_is_never_misidentified_as_orphan(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        queued = QueuedRequest(asyncio.Event(), time.monotonic())
        supervisor.queued_requests["handoff"] = queued
        await supervisor._acquire_generation_slot("handoff")

        status = supervisor.status()

        self.assertEqual(status["generationLockState"], "handoff")
        self.assertEqual(status["generationLockRecoveries"], 0)
        self.assertTrue(supervisor.generation_lock.locked())
        self.assertTrue(supervisor._release_generation_slot("handoff"))
        supervisor.queued_requests.clear()

    async def test_wrong_request_cannot_release_another_requests_slot(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        await supervisor._acquire_generation_slot("owner")

        self.assertFalse(supervisor._release_generation_slot("other"))
        self.assertTrue(supervisor.generation_lock.locked())
        self.assertEqual(supervisor.generation_owner, "owner")
        self.assertTrue(supervisor._release_generation_slot("owner"))

    async def test_waiter_already_queued_wakes_after_orphan_recovery(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        await supervisor.generation_lock.acquire()
        supervisor.generation_owner = "vanished"
        supervisor.queued_requests["waiting"] = QueuedRequest(asyncio.Event(), time.monotonic())
        acquire = asyncio.create_task(supervisor._acquire_generation_slot("waiting"))
        await asyncio.sleep(0)

        self.assertTrue(supervisor._recover_orphaned_generation_slot("test"))
        await acquire

        self.assertEqual(supervisor.generation_owner, "waiting")
        self.assertTrue(supervisor.generation_lock.locked())
        supervisor.queued_requests.clear()
        self.assertTrue(supervisor._release_generation_slot("waiting"))

    async def test_many_disconnects_never_leave_generation_slot_locked(self):
        supervisor = StressSupervisor(self.root, self.settings)
        supervisor.settings.data["generation"]["streamHeartbeatSeconds"] = 0.001
        supervisor.loaded = {"id": "model", "name": "model", "engine": "lm-studio"}

        async def consume(index: int):
            async for _event in supervisor.generate(str(index), [], {}, f"request-{index}"):
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(consume(index)) for index in range(100)]
        await asyncio.sleep(0.01)
        for task in tasks[::3]:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        status = supervisor.status()
        self.assertEqual(status["activeRequestCount"], 0)
        self.assertEqual(status["queuedRequestCount"], 0)
        self.assertEqual(status["generationLockState"], "idle")
        self.assertFalse(supervisor.generation_lock.locked())
        self.assertIsNone(supervisor.generation_owner)

    async def test_queue_full_and_timeout_are_bounded_errors(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        limits = supervisor.settings.data["generation"]
        limits["streamHeartbeatSeconds"] = 0.005
        limits["maxQueuedRequests"] = 1
        limits["queueTimeoutSeconds"] = 0.02
        supervisor.loaded = {"id": "model", "name": "model", "engine": "lm-studio"}
        first = supervisor.generate("first", [], {}, "first")
        first_event = asyncio.create_task(anext(first))
        await supervisor.stream_started.wait()
        queued = supervisor.generate("queued", [], {}, "queued")
        await anext(queued)
        overflow = supervisor.generate("overflow", [], {}, "overflow")
        with self.assertRaises(MLXBarError) as full:
            await anext(overflow)
        self.assertEqual(full.exception.code, "QUEUE_FULL")
        with self.assertRaises(MLXBarError) as timeout:
            while True:
                await anext(queued)
        self.assertEqual(timeout.exception.code, "QUEUE_TIMEOUT")
        supervisor.stream_release.set()
        await first_event
        await first.aclose()

    async def test_cancel_all_cancels_every_queued_generation(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.settings.data["generation"]["streamHeartbeatSeconds"] = 0.01
        supervisor.settings.data["generation"]["cancelGraceSeconds"] = 0.01
        supervisor.loaded = {"id": "model", "name": "model", "engine": "lm-studio"}
        first = supervisor.generate("first", [], {}, "first")
        first_event = asyncio.create_task(anext(first))
        await supervisor.stream_started.wait()
        second = supervisor.generate("second", [], {}, "second")
        third = supervisor.generate("third", [], {}, "third")
        await anext(second)
        await anext(third)
        result = await supervisor.cancel_all()
        self.assertEqual(result["queuedCancelled"], 2)
        self.assertEqual(result["activeCancelled"], 1)
        self.assertTrue(result["cancelled"])
        self.assertEqual((await anext(second))["finish_reason"], "cancelled")
        self.assertEqual((await anext(third))["finish_reason"], "cancelled")
        await second.aclose()
        await third.aclose()
        await asyncio.gather(first_event, return_exceptions=True)
        await first.aclose()

    async def test_cancel_all_management_route_is_not_treated_as_request_id(self):
        worker = SimpleNamespace(cancel_all=AsyncMock(return_value={"cancelled": True}))
        client = TestClient(make_management_app(SimpleNamespace(workers=worker)))
        response = client.post("/api/v1/generate/cancel-all")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["cancelled"])
        worker.cancel_all.assert_awaited_once()

    async def test_management_sse_disconnect_explicitly_closes_worker_generation(self):
        closed = asyncio.Event()

        class Worker:
            async def generate(self, *_args, **_kwargs):
                try:
                    yield {"type": "phase", "name": "prefill"}
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(mlxbar=SimpleNamespace(workers=Worker()))))
        response = await management_generate(request, {"prompt": "hello", "requestId": "r"})
        iterator = response.body_iterator
        first = await anext(iterator)
        self.assertIn("prefill", first.decode() if isinstance(first, bytes) else first)

        await iterator.aclose()

        self.assertTrue(closed.is_set())

    async def test_api_log_stream_wrapper_propagates_disconnect_to_inner_stream(self):
        closed = asyncio.Event()

        async def inner_stream():
            try:
                yield b"first"
                await asyncio.Event().wait()
            finally:
                closed.set()

        database = SimpleNamespace(add_api_log=lambda _entry: None)
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}),
            workers=SimpleNamespace(), database=database)
        app = make_public_app(state)
        middleware = app.user_middleware[0].kwargs["dispatch"]
        request = SimpleNamespace(
            method="POST", url=SimpleNamespace(path="/v1/chat/completions"),
            client=SimpleNamespace(host="127.0.0.1"), state=SimpleNamespace())
        response = StreamingResponse(inner_stream())
        wrapped = await middleware(request, lambda _request: asyncio.sleep(0, result=response))

        self.assertEqual(await anext(wrapped.body_iterator), b"first")
        await wrapped.body_iterator.aclose()

        self.assertTrue(closed.is_set())

    async def test_prompt_cache_management_routes_use_public_worker_methods(self):
        worker = SimpleNamespace(
            prompt_cache_stats=AsyncMock(return_value={"disk": True, "disk_bytes": 1024}),
            clear_memory_prompt_cache=AsyncMock(return_value={"memory": True}),
            clear_disk_prompt_cache=AsyncMock(return_value={"disk": True, "disk_bytes": 0}),
        )
        client = TestClient(make_management_app(SimpleNamespace(workers=worker)))
        self.assertEqual(client.get("/api/v1/prompt-cache").json()["disk_bytes"], 1024)
        self.assertTrue(client.post("/api/v1/prompt-cache/memory/clear").json()["memory"])
        self.assertEqual(client.post("/api/v1/prompt-cache/disk/clear").json()["disk_bytes"], 0)
        worker.prompt_cache_stats.assert_awaited_once()
        worker.clear_memory_prompt_cache.assert_awaited_once()
        worker.clear_disk_prompt_cache.assert_awaited_once()

    async def test_frozen_distribution_finds_bundled_worker_modules(self):
        frozen = self.root / "_internal"
        workers = frozen / "Workers"
        workers.mkdir(parents=True)
        with patch("mlxbar.workers.supervisor.sys._MEIPASS", str(frozen), create=True):
            paths = WorkerSupervisor._worker_import_paths()
        self.assertEqual(paths[0], str(workers))

    async def test_loading_status_names_model_and_clears_after_completion(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        load_started = asyncio.Event()
        load_release = asyncio.Event()

        async def start_worker(_engine):
            return None

        async def rpc(method, _params, timeout=30):
            self.assertEqual(method, "load")
            load_started.set()
            await load_release.wait()
            return {"capabilities": {"streaming": True}}

        supervisor._start_worker = start_worker
        supervisor._call = rpc
        task = asyncio.create_task(supervisor.load(
            {"id": "model-id", "name": "Visible Model", "engine": "mlx-lm", "path": "/model"}
        ))
        await load_started.wait()
        status = supervisor.status()
        self.assertEqual(status["loadingModel"]["name"], "Visible Model")
        self.assertEqual(status["loadingModel"]["phase"], "モデルデータを読み込み中")
        load_release.set()
        result = await task
        self.assertEqual(result["name"], "Visible Model")
        self.assertIsNone(supervisor.status()["loadingModel"])

    async def test_incompatible_local_model_falls_back_to_matching_lm_studio_provider(self):
        supervisor = ControlledSupervisor(self.root, self.settings)

        attempted = []

        async def start_worker(engine):
            attempted.append(engine)
            supervisor.engine = engine

        async def rpc(method, _params, timeout=30):
            self.assertEqual(method, "load")
            if supervisor.engine in {"mlx-lm", "mlx-vlm"}:
                raise MLXBarError("MODEL_INCOMPATIBLE", "unsupported", 400)
            return {"capabilities": {}}

        supervisor._start_worker = start_worker
        supervisor._call = rpc
        supervisor._load_lmstudio = AsyncMock(return_value={"status": "loaded", "instance_id": "laguna-test"})
        result = await supervisor.load({
            "id": "local-model", "name": "Laguna-S-2.1-oQ2e", "engine": "mlx-lm",
            "path": "/model", "provider_key": "laguna-s-2.1-oq2e",
        })

        self.assertEqual(result["engine"], "lm-studio")
        self.assertEqual(result["provider_instance_id"], "laguna-test")
        self.assertEqual(result["provider_key"], "laguna-s-2.1-oq2e")
        self.assertTrue(supervisor.stopped)
        self.assertEqual(attempted, ["mlx-lm", "mlx-vlm"])
        supervisor._load_lmstudio.assert_awaited_once()

    async def test_incompatible_mlx_lm_model_retries_with_mlx_vlm(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        attempted = []

        async def start_worker(engine):
            attempted.append(engine)
            supervisor.engine = engine

        async def rpc(method, _params, timeout=30):
            self.assertEqual(method, "load")
            if supervisor.engine == "mlx-lm":
                raise MLXBarError("MODEL_INCOMPATIBLE", "unsupported", 400)
            return {"capabilities": {"modalities": ["text"]}}

        supervisor._start_worker = start_worker
        supervisor._call = rpc
        result = await supervisor.load({
            "id": "future-model", "name": "Future Model", "engine": "mlx-lm", "path": "/model",
        })
        self.assertEqual(attempted, ["mlx-lm", "mlx-vlm"])
        self.assertEqual(result["engine"], "mlx-vlm")
        self.assertEqual(result["capabilities"]["modalities"], ["text"])

    async def test_lmstudio_load_error_preserves_provider_message(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        response = httpx.Response(400, content=b'{"error":{"message":"not enough memory"}}')
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        with patch("mlxbar.workers.supervisor.httpx.AsyncClient", return_value=client):
            with self.assertRaises(MLXBarError) as raised:
                await supervisor._load_lmstudio({"provider_key": "large-model"}, 30)
        self.assertEqual(raised.exception.code, "LMSTUDIO_LOAD_FAILED")
        self.assertIn("not enough memory", raised.exception.message)

    async def test_cancel_forces_worker_when_stream_does_not_finish(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.loaded = {"id": "model", "name": "model", "engine": "mlx-lm"}
        supervisor.settings.data["generation"]["cancelGraceSeconds"] = 0.01
        supervisor.active_requests["stuck"] = ActiveRequest(
            asyncio.Event(), asyncio.Event(), None, "mlx-lm"
        )

        async def rpc(_method, _params, timeout=30):
            return {"cancelled": True}

        supervisor._call = rpc
        result = await supervisor.cancel("stuck")
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["forced"])
        self.assertTrue(supervisor.stopped)
        self.assertIsNone(supervisor.loaded)

    async def test_generation_limits_clamp_oversized_tokens_and_reject_invalid_inputs(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        limit = self.settings.data["generation"]["maxPromptCharacters"]
        with self.assertRaises(MLXBarError) as prompt_error:
            supervisor._validate_generation("x" * (limit + 1), [], {})
        self.assertEqual(prompt_error.exception.code, "INPUT_TOO_LARGE")
        with self.assertRaises(MLXBarError):
            supervisor._validate_generation("ok", "not-a-list", {})
        _, _, options = supervisor._validate_generation("ok", [], {"max_tokens": 999999})
        self.assertEqual(options["max_tokens"], 8192)
        self.assertEqual(options["temperature"], 0.7)
        self.assertEqual(options["top_p"], 1.0)
        self.assertEqual(options["repetition_penalty"], 1.0)
        self.assertEqual(options["repetition_context_size"], 20)
        _, _, custom = supervisor._validate_generation("ok", [], {
            "temperature": 0.2, "top_p": 0.85, "repetition_penalty": 1.15,
            "repetition_context_size": 128, "presence_penalty": 0.3, "frequency_penalty": 0.4,
        })
        self.assertEqual(custom["temperature"], 0.2)
        self.assertEqual(custom["top_p"], 0.85)
        self.assertEqual(custom["repetition_penalty"], 1.15)
        self.assertEqual(custom["repetition_context_size"], 128)
        with self.assertRaises(MLXBarError):
            supervisor._validate_generation("ok", [], {"max_tokens": 0})
        for invalid in ({"temperature": 2.1}, {"top_p": 1.1}, {"repetition_penalty": 0},
                        {"repetition_context_size": 0}, {"frequency_penalty": 2.1}):
            with self.assertRaises(MLXBarError):
                supervisor._validate_generation("ok", [], invalid)

    async def test_model_limit_is_detected_and_combined_with_user_limit(self):
        model = self.root / "model"
        model.mkdir()
        (model / "config.json").write_text(
            '{"text_config":{"max_position_embeddings":4096}}', encoding="utf-8"
        )
        supervisor = ControlledSupervisor(self.root, self.settings)
        self.assertEqual(supervisor._detect_model_max_tokens(str(model)), 4096)
        supervisor.loaded = {"capabilities": {"modelMaxTokens": 4096}}
        self.settings.data["generation"]["maxTokens"] = 32768
        self.assertEqual(supervisor.effective_max_tokens(), 4096)
        _, _, options = supervisor._validate_generation("ok", [], {"max_tokens": 32768})
        self.assertEqual(options["max_tokens"], 4096)

    async def test_user_limit_can_be_raised_above_legacy_8192_limit(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        self.settings.data["generation"]["maxTokens"] = 32768
        supervisor.loaded = {"capabilities": {"modelMaxTokens": 1048576}}
        _, _, options = supervisor._validate_generation("ok", [], {"max_tokens": 65536})
        self.assertEqual(options["max_tokens"], 32768)

    async def test_large_zcode_history_uses_model_context_instead_of_legacy_character_limit(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.loaded = {"capabilities": {"modelMaxTokens": 1048576}}
        self.assertEqual(supervisor.effective_max_prompt_characters(), 4194304)
        messages = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "x" * 200000},
        ]
        normalized, _, _ = supervisor._validate_generation(messages, [], {"max_tokens": 512})
        self.assertEqual(normalized, messages)

    async def test_prompt_preflight_remains_bounded_for_unknown_models(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        self.assertEqual(supervisor.effective_max_prompt_characters(), 100000)
        with self.assertRaises(MLXBarError) as raised:
            supervisor._validate_generation("x" * 100001, [], {})
        self.assertEqual(raised.exception.code, "INPUT_TOO_LARGE")

    async def test_memory_pressure_rejects_generation_before_inference(self):
        supervisor = ControlledSupervisor(self.root, self.settings)

        async def memory(_method, _params, timeout=30):
            return {"memory": {"active_bytes": 80, "cache_bytes": 15,
                               "physical_memory_bytes": 100}}

        supervisor._call = memory
        with self.assertRaises(MLXBarError) as raised:
            await supervisor._ensure_memory_capacity()
        self.assertEqual(raised.exception.code, "MEMORY_PRESSURE")

    async def test_memory_pressure_discards_prompt_cache_before_rejecting(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        calls = []

        async def memory(method, _params, timeout=30):
            calls.append(method)
            if method == "clear_prompt_cache":
                return {"type": "completed"}
            used = 95 if calls.count("memory") == 1 else 70
            return {"memory": {"active_bytes": used, "cache_bytes": 0,
                               "physical_memory_bytes": 100}}

        supervisor._call = memory
        await supervisor._ensure_memory_capacity()
        self.assertEqual(calls, ["memory", "clear_prompt_cache", "memory"])


class ErrorWorker:
    loaded = {"id": "model"}

    async def generate(self, *_args, **_kwargs):
        yield {"type": "error", "code": "GENERATION_FAILED",
               "message": "synthetic failure", "retryable": False}


class OpenAIErrorTests(unittest.TestCase):
    def setUp(self):
        state = SimpleNamespace(
            settings=SimpleNamespace(data={"api": {"requireToken": False}}),
            workers=ErrorWorker(),
        )
        self.client = TestClient(make_public_app(state), raise_server_exceptions=False)

    def test_non_streaming_generation_error_is_not_success(self):
        response = self.client.post("/v1/chat/completions", json={
            "model": "model", "messages": [{"role": "user", "content": "hello"}]
        })
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "GENERATION_FAILED")

    def test_streaming_generation_error_is_forwarded(self):
        response = self.client.post("/v1/chat/completions", json={
            "model": "model", "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('"code": "GENERATION_FAILED"', response.text)
        self.assertIn("data: [DONE]", response.text)

    def test_malformed_messages_are_rejected_cleanly(self):
        response = self.client.post("/v1/chat/completions", json={
            "model": "model", "messages": ["invalid"]
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()


class V150HardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _drain(self, supervisor, request_id):
        events = []
        async for event in supervisor.generate("hi", [], {"temperature": 0.5, "top_p": 1.0}, request_id):
            events.append(event)
        return events

    async def test_unload_while_queued_fails_cleanly_and_frees_the_slot(self):
        """A waiter whose model disappeared must get a real error, not a crash.

        The entry check runs before the request queues, so an unload in between
        used to reach `self.loaded.get(...)` on None -- outside the block that
        releases the generation slot, leaving the lock inconsistent until the
        orphan recovery noticed.
        """
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.settings.data["generation"]["streamHeartbeatSeconds"] = 1
        supervisor.loaded = {"id": "m", "name": "m", "engine": "lm-studio"}
        first = asyncio.create_task(self._drain(supervisor, "first"))
        await supervisor.stream_started.wait()
        second = asyncio.create_task(self._drain(supervisor, "second"))
        await asyncio.sleep(0.1)
        self.assertIn("second", supervisor.queued_requests)

        await supervisor.unload()
        supervisor.stream_release.set()
        await first
        with self.assertRaises(MLXBarError) as caught:
            await second
        self.assertEqual(caught.exception.code, "MODEL_NOT_LOADED")
        self.assertEqual(caught.exception.status, 409)
        # Released by the request's own finalizer, not by orphan recovery.
        self.assertFalse(supervisor.generation_lock.locked())
        self.assertIsNone(supervisor.generation_owner)
        self.assertEqual(supervisor.generation_lock_recoveries, 0)
        self.assertEqual(supervisor.status()["generationLockState"], "idle")

    async def test_management_load_and_unload_refuse_while_requests_are_in_flight(self):
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.loaded = {"id": "m", "name": "m", "engine": "lm-studio"}
        state = SimpleNamespace(workers=supervisor)
        from mlxbar.api.management import _raise_if_generations_in_flight

        supervisor.queued_requests["waiting"] = QueuedRequest(asyncio.Event(), time.monotonic())
        with self.assertRaises(HTTPException) as caught:
            _raise_if_generations_in_flight(state, False, "モデルを切り替え")
        self.assertEqual(caught.exception.detail["code"], "ENGINE_BUSY")
        self.assertEqual(caught.exception.detail["queuedRequestCount"], 1)
        # The GUI still needs a way through.
        _raise_if_generations_in_flight(state, True, "モデルを切り替え")
        supervisor.queued_requests.clear()
        _raise_if_generations_in_flight(state, False, "モデルを切り替え")

    async def test_long_generation_times_out_without_unloading_the_model(self):
        """A slow reply is not a broken worker; reloading a 27B helps nobody."""
        supervisor = ControlledSupervisor(self.root, self.settings)
        supervisor.settings.data["generation"]["totalTimeoutSeconds"] = 10
        supervisor.loaded = {"id": "m", "name": "m", "engine": "mlx-lm"}
        supervisor.socket_path = self.root / "worker.sock"

        with self.assertRaises(MLXBarError) as caught:
            async for _ in supervisor.generate("hi", [], {"temperature": 0.5, "top_p": 1.0}, "slow"):
                pass
        # Reaching the worker fails in this harness; what matters is that the
        # stalled path and the merely-slow path are no longer the same branch.
        self.assertIn(caught.exception.code, {"WORKER_CRASHED", "WORKER_STALLED", "GENERATION_TIMEOUT"})

    async def test_stalled_worker_is_detected_by_the_idle_watchdog(self):
        async def silent_lines():
            await asyncio.sleep(5)
            yield "never"

        with self.assertRaises(WorkerStalled):
            async for _ in WorkerSupervisor._lines_with_idle_timeout(silent_lines(), 0.05):
                pass

    async def test_idle_watchdog_passes_lines_through_while_they_keep_coming(self):
        async def lines():
            for index in range(3):
                await asyncio.sleep(0.01)
                yield f"line{index}"

        received = [line async for line in WorkerSupervisor._lines_with_idle_timeout(lines(), 1.0)]
        self.assertEqual(received, ["line0", "line1", "line2"])

    async def test_memory_pressure_uses_free_memory_and_the_os_verdict(self):
        healthy = {"active_bytes": 10, "cache_bytes": 0, "physical_memory_bytes": 100,
                   "available_bytes": 50, "pressure_level": 1, "process_rss_bytes": 10}
        self.assertIsNone(WorkerSupervisor.memory_pressure_reason(healthy, 0.9))
        self.assertTrue(WorkerSupervisor.memory_pressure_reason({**healthy, "available_bytes": 2}, 0.9))
        self.assertTrue(WorkerSupervisor.memory_pressure_reason({**healthy, "pressure_level": 4}, 0.9))
        self.assertTrue(WorkerSupervisor.memory_pressure_reason({**healthy, "process_rss_bytes": 95}, 0.9))

    async def test_completed_job_does_not_retain_its_event_queue(self):
        """A months-long coordinator must not accumulate one queue per job."""
        from mlxbar.database import Database
        from mlxbar.jobs import JobManager

        database = Database(self.root / "jobs.sqlite3")
        manager = JobManager(database)

        async def work(update):
            await update(0.5, "半分")
            return {"ok": True}

        job = manager.create("model_scan", work)
        for _ in range(200):
            if job["id"] not in manager.tasks:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(database.get_job(job["id"])["state"], "completed")
        self.assertNotIn(job["id"], manager.queues)
        database.close()
