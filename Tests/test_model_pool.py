from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mlxbar.errors import MLXBarError
from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


class FakeWorker:
    instances = []
    load_gate: asyncio.Event | None = None
    load_calls = 0
    observed_bytes = 768 << 20

    def __init__(self, root, settings, **kwargs):
        self.root = root
        self.settings = settings
        self.loaded = None
        self.loading = None
        self.active_requests = {}
        self.queued_requests = {}
        self.maintenance_engines = set()
        self.memory_limit_bytes = kwargs.get("memory_limit_bytes", 0)
        self.process = None
        self.socket_path = None
        self.engine = None
        self.unloaded = False
        FakeWorker.instances.append(self)

    @staticmethod
    def _is_our_worker(pid): return False

    async def load(self, model, engine=None):
        FakeWorker.load_calls += 1
        if FakeWorker.load_gate is not None:
            await FakeWorker.load_gate.wait()
        self.loaded = {**model, "engine": engine or model.get("engine"),
                       "capabilities": {"modelMaxTokens": 4096, "memoryLimits": {
                           "set_memory_limit": self.memory_limit_bytes}}}
        self.engine = self.loaded["engine"]
        return self.loaded

    async def unload(self):
        self.unloaded = True
        self.loaded = None
        return {"state": "unloaded"}

    async def shutdown(self):
        self.loaded = None

    async def _call(self, method, params, timeout=5):
        return {"memory": {"process_rss_bytes": FakeWorker.observed_bytes,
                           "active_bytes": 512 << 20,
                           "cache_bytes": 0}}

    async def generate(self, prompt, images, options, request_id=None, image_root=None):
        self.active_requests[request_id] = object()
        try:
            await asyncio.sleep(0)
            yield {"type": "delta", "text": str(prompt)}
        finally:
            self.active_requests.pop(request_id, None)

    async def cancel(self, request_id): return {"cancelled": request_id in self.active_requests}
    async def prompt_cache_stats(self): return {"enabled": False}
    async def clear_memory_prompt_cache(self): return {"enabled": False}
    async def clear_disk_prompt_cache(self): return {"enabled": False}
    async def wait_until_idle(self, timeout=30): return True
    async def probe_runtime(self, engine): return {"ok": True, "engine": engine}
    def begin_maintenance(self, engine): self.maintenance_engines.add(engine)
    def end_maintenance(self, engine): self.maintenance_engines.discard(engine)
    def effective_max_tokens(self): return 4096
    def effective_max_prompt_characters(self): return 16384
    def raise_if_queue_full(self): return None
    def status(self):
        return {"loadedModel": self.loaded, "loadingModel": self.loading,
                "activeRequestCount": len(self.active_requests),
                "queuedRequestCount": len(self.queued_requests)}


class ModelPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 1, "defaultPerModelMaxGB": 8,
            "idleTTLSeconds": 30,
        })
        FakeWorker.instances = []
        FakeWorker.load_gate = None
        FakeWorker.load_calls = 0
        FakeWorker.observed_bytes = 768 << 20
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker)
        self.worker_patch.start()
        self.pool = ModelPoolSupervisor(self.root, self.settings)
        self.pool._physical_memory = lambda: 32 * GIB
        self.pool._host_capacity = lambda: (24 * GIB, 1)

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def model(number, size_gb=1):
        return {"id": f"model-{number}", "name": f"model-{number}",
                "engine": "mlx-lm", "size_bytes": int(size_gb * GIB)}

    async def test_two_models_stay_in_independent_workers(self):
        one = await self.pool.load_for_api(self.model(1))
        two = await self.pool.load_for_api(self.model(2))
        self.assertEqual({item["id"] for item in self.pool.loaded_models()}, {"model-1", "model-2"})
        self.assertIsNot(self.pool._slots[one["id"]].worker, self.pool._slots[two["id"]].worker)
        self.assertFalse(self.pool._slots[one["id"]].worker.unloaded)

    async def test_concurrent_requests_singleflight_one_load(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        first = asyncio.create_task(self.pool.load_for_api(self.model(1)))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.pool.load_for_api(self.model(1)))
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(FakeWorker.load_calls, 1)
        self.assertEqual(results[0]["id"], results[1]["id"])

    async def test_third_model_evicts_oldest_idle_api_model(self):
        await self.pool.load_for_api(self.model(1))
        self.pool._slots["model-1"].last_released_at = time.monotonic() - 20
        await self.pool.load_for_api(self.model(2))
        await self.pool.load_for_api(self.model(3))
        self.assertNotIn("model-1", self.pool._slots)
        self.assertEqual(set(self.pool._slots), {"model-2", "model-3"})

    async def test_manual_pin_is_never_lru_evicted(self):
        await self.pool.load(self.model(1), pin=True)
        await self.pool.load_for_api(self.model(2))
        await self.pool.load_for_api(self.model(3))
        self.assertIn("model-1", self.pool._slots)
        self.assertNotIn("model-2", self.pool._slots)

    async def test_ttl_reaps_only_unpinned_idle_models(self):
        await self.pool.load_for_api(self.model(1))
        await self.pool.load(self.model(2), pin=True)
        for slot in self.pool._slots.values():
            slot.last_released_at = time.monotonic() - 31
        self.assertEqual(await self.pool._reap_once(), 1)
        self.assertEqual(set(self.pool._slots), {"model-2"})

    async def test_critical_pressure_overrides_idle_pin(self):
        await self.pool.load(self.model(1), pin=True)
        self.pool._host_capacity = lambda: (1 * GIB, 4)
        self.assertEqual(await self.pool._reap_once(), 1)
        self.assertFalse(self.pool._slots)

    async def test_per_model_limit_rejects_before_worker_creation(self):
        self.settings.data["models"]["pool"]["defaultPerModelMaxGB"] = 1
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(self.model(1, size_gb=1))
        self.assertEqual(raised.exception.code, "MODEL_MEMORY_LIMIT")
        self.assertEqual(len(FakeWorker.instances), 1)  # compatibility worker only

    async def test_allocator_limit_is_the_admitted_reservation_not_the_machine_cap(self):
        await self.pool.load_for_api(self.model(1))
        slot = self.pool._slots["model-1"]
        self.assertEqual(slot.worker.memory_limit_bytes, slot.reservation_bytes)
        self.assertLess(slot.worker.memory_limit_bytes, 8 * GIB)

    async def test_post_load_measurement_cannot_exceed_the_admitted_reservation(self):
        FakeWorker.observed_bytes = 3 * GIB
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(self.model(1))
        self.assertEqual(raised.exception.code, "MEMORY_BUDGET_EXCEEDED")
        self.assertFalse(self.pool._slots)

    async def test_generation_lease_is_released_when_stream_closes(self):
        await self.pool.load_for_api(self.model(1))
        generation = self.pool.generate_for_model("model-1", "hello", [], {}, "request")
        self.assertEqual((await anext(generation))["text"], "hello")
        await generation.aclose()
        self.assertEqual(self.pool._slots["model-1"].leases, 0)

    async def test_enabled_toggle_is_latched_until_service_restart(self):
        self.settings.data["models"]["pool"]["enabled"] = False
        self.assertTrue(self.pool.enabled)
        status = self.pool.status()["modelPool"]
        self.assertFalse(status["configuredEnabled"])
        self.assertTrue(status["restartRequired"])

    async def test_live_resident_reduction_evicts_oldest_unpinned_model(self):
        await self.pool.load_for_api(self.model(1))
        self.pool._slots["model-1"].last_released_at = time.monotonic() - 20
        await self.pool.load_for_api(self.model(2))
        self.settings.data["models"]["pool"]["maxResidentModels"] = 1
        self.assertEqual(await self.pool._reap_once(), 1)
        self.assertEqual(set(self.pool._slots), {"model-2"})

    async def test_dead_idle_worker_is_removed_even_when_pinned(self):
        await self.pool.load(self.model(1), pin=True)
        worker = self.pool._slots["model-1"].worker
        worker.status = lambda: {"workerRunning": False, "loadedModel": None,
                                 "loadingModel": None}
        self.assertEqual(await self.pool._reap_once(), 1)
        self.assertFalse(self.pool._slots)

    async def test_unload_waits_for_an_inflight_cold_load(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        loading = asyncio.create_task(self.pool.load_for_api(self.model(1)))
        await asyncio.sleep(0)
        unloading = asyncio.create_task(self.pool.unload())
        await asyncio.sleep(0)
        self.assertFalse(unloading.done())
        gate.set()
        await loading
        result = await unloading
        self.assertEqual(result["count"], 1)
        self.assertFalse(self.pool._slots)

    async def test_ttl_starts_when_a_slow_load_finishes(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        loading = asyncio.create_task(self.pool.load_for_api(self.model(1)))
        await asyncio.sleep(0)
        self.pool._slots["model-1"].last_released_at = time.monotonic() - 100
        gate.set()
        await loading
        self.assertEqual(await self.pool._reap_once(), 0)
        self.assertIn("model-1", self.pool._slots)
