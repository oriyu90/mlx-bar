from __future__ import annotations

import asyncio
import contextlib
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
        # Concurrency probes: tests set gen_release to an Event the fake will
        # block on before emitting its first delta, so overlap is observable.
        self.gen_started: asyncio.Event | None = None
        self.gen_release: asyncio.Event | None = None
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
            if self.gen_started is not None:
                self.gen_started.set()
            if self.gen_release is not None:
                await self.gen_release.wait()
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


class CrossModelGenerationTests(unittest.IsolatedAsyncioTestCase):
    """v1.7.0: distinct resident models generate concurrently; same model does not."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 3, "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 1, "defaultPerModelMaxGB": 8, "idleTTLSeconds": 30,
        })
        FakeWorker.instances = []
        FakeWorker.load_gate = None
        FakeWorker.load_calls = 0
        FakeWorker.observed_bytes = 768 << 20
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", FakeWorker)
        self.worker_patch.start()
        self._pools: list[ModelPoolSupervisor] = []

    async def asyncTearDown(self):
        for pool in self._pools:
            await pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    def make_pool(self, concurrency: int | None = None) -> ModelPoolSupervisor:
        if concurrency is not None:
            self.settings.data["models"]["pool"]["generationConcurrency"] = concurrency
        pool = ModelPoolSupervisor(self.root, self.settings)
        pool._physical_memory = lambda: 32 * GIB
        pool._host_capacity = lambda: (24 * GIB, 1)
        self._pools.append(pool)
        return pool

    @staticmethod
    def model(number):
        return {"id": f"model-{number}", "name": f"model-{number}",
                "engine": "mlx-lm", "size_bytes": GIB}

    @staticmethod
    async def collect(generation) -> list[dict]:
        events = []
        async for event in generation:
            events.append(event)
        return events

    async def test_default_generation_concurrency_is_two(self):
        pool = self.make_pool()
        self.assertEqual(pool._gen_concurrency, 2)

    async def test_distinct_models_generate_concurrently(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        await pool.load(self.model(2))
        w1 = pool._slots["model-1"].worker
        w2 = pool._slots["model-2"].worker
        w1.gen_release = asyncio.Event()
        w2.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        g2 = pool.generate_for_model("model-2", "b", [], {}, "r2")
        t1 = asyncio.create_task(anext(g1))
        t2 = asyncio.create_task(anext(g2))
        await asyncio.sleep(0.05)
        # Both are inside their worker's generate() at the same time.
        self.assertTrue(w1.active_requests)
        self.assertTrue(w2.active_requests)
        self.assertEqual(pool._gen_active_lanes, 2)
        w1.gen_release.set()
        w2.gen_release.set()
        self.assertEqual((await t1)["text"], "a")
        self.assertEqual((await t2)["text"], "b")
        await g1.aclose()
        await g2.aclose()
        self.assertEqual(pool._gen_active_lanes, 0)

    async def test_same_model_requests_stay_serialised(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        w1 = pool._slots["model-1"].worker
        w1.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        t1 = asyncio.create_task(anext(g1))
        await asyncio.sleep(0.02)
        g2 = pool.generate_for_model("model-1", "b", [], {}, "r2")
        t2 = asyncio.create_task(self.collect(g2))
        await asyncio.sleep(0.02)
        # r2 cannot start while r1 owns the model lane.
        self.assertIn("r2", pool._slots["model-1"].gen_queued)
        self.assertEqual(len(w1.active_requests), 1)
        w1.gen_release.set()
        self.assertEqual((await t1)["text"], "a")
        await g1.aclose()
        events = await t2
        self.assertEqual(events[-1]["text"], "b")

    async def test_concurrency_one_serialises_across_models(self):
        pool = self.make_pool(concurrency=1)
        self.assertEqual(pool._gen_concurrency, 1)
        await pool.load(self.model(1))
        await pool.load(self.model(2))
        w1 = pool._slots["model-1"].worker
        w1.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        t1 = asyncio.create_task(anext(g1))
        await asyncio.sleep(0.02)
        g2 = pool.generate_for_model("model-2", "b", [], {}, "r2")
        t2 = asyncio.create_task(self.collect(g2))
        await asyncio.sleep(0.02)
        # The single permit is held by r1; r2 waits even though it is a
        # different model -- byte-identical to the pre-v1.7.0 global lock.
        self.assertIn("r2", pool._slots["model-2"].gen_queued)
        self.assertFalse(pool._slots["model-2"].worker.active_requests)
        w1.gen_release.set()
        await t1
        await g1.aclose()
        events = await t2
        self.assertEqual(events[-1]["text"], "b")

    async def test_memory_guard_downgrades_second_lane_to_queue_without_failing(self):
        pool = self.make_pool()
        # Force the head-room charge to blow the budget for any second lane.
        pool._per_generation_headroom = lambda slot: 999 * GIB
        await pool.load(self.model(1))
        await pool.load(self.model(2))
        w1 = pool._slots["model-1"].worker
        w1.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        t1 = asyncio.create_task(anext(g1))
        await asyncio.sleep(0.02)
        g2 = pool.generate_for_model("model-2", "b", [], {}, "r2")
        t2 = asyncio.create_task(self.collect(g2))
        await asyncio.sleep(0.02)
        self.assertIn("r2", pool._slots["model-2"].gen_queued)  # queued, not errored
        w1.gen_release.set()
        await t1
        await g1.aclose()
        events = await t2  # completes once the first lane frees
        self.assertEqual(events[-1]["text"], "b")

    async def test_orphaned_lane_is_recovered(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        slot = pool._slots["model-1"]
        await slot.gen_lock.acquire()
        slot.gen_owner = "vanished"
        slot.gen_permit = True
        pool._gen_active_lanes = 1
        self.assertTrue(pool._recover_lane(slot, "test"))
        self.assertFalse(slot.gen_lock.locked())
        self.assertIsNone(slot.gen_owner)
        self.assertEqual(pool._gen_active_lanes, 0)
        self.assertEqual(slot.gen_recoveries, 1)

    async def test_queued_request_can_be_cancelled_per_model(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        w1 = pool._slots["model-1"].worker
        w1.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        t1 = asyncio.create_task(anext(g1))
        await asyncio.sleep(0.02)
        g2 = pool.generate_for_model("model-1", "b", [], {}, "r2")
        t2 = asyncio.create_task(self.collect(g2))
        await asyncio.sleep(0.02)
        result = await pool.cancel("r2")
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["queued"])
        events = await t2
        self.assertEqual(events[-1]["finish_reason"], "cancelled")
        w1.gen_release.set()
        await t1
        await g1.aclose()

    async def test_status_reports_concurrency_and_active_lanes(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        await pool.load(self.model(2))
        w1 = pool._slots["model-1"].worker
        w1.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        t1 = asyncio.create_task(anext(g1))
        await asyncio.sleep(0.02)
        status = pool.status()
        self.assertEqual(status["generationConcurrency"], 2)
        self.assertEqual(status["activeGenerations"], 1)
        self.assertEqual(status["modelPool"]["generationConcurrency"], 2)
        w1.gen_release.set()
        await t1
        await g1.aclose()

    async def test_status_reports_per_model_generation_rate(self):
        # v1.9.0: the menu bar shows a live tok/s under every resident model,
        # not just the primary, so each `loadedModels[]` row carries its own
        # rate and the top-level field mirrors it for the existing header line.
        pool = self.make_pool()
        await pool.load(self.model(1))
        await pool.load(self.model(2))
        w1 = pool._slots["model-1"].worker
        base_status = w1.status
        w1.status = lambda: {**base_status(),
                             "generationTokensPerSecond": 42.4,
                             "generatedTokens": 128}
        status = pool.status()
        row1 = next(m for m in status["loadedModels"] if m["id"] == "model-1")
        self.assertEqual(row1["generationTokensPerSecond"], 42.4)
        self.assertEqual(row1["generatedTokens"], 128)
        row2 = next(m for m in status["loadedModels"] if m["id"] == "model-2")
        self.assertNotIn("generationTokensPerSecond", row2)
        self.assertEqual(status["generationTokensPerSecond"], 42.4)

    async def test_status_has_no_generation_rate_when_idle(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        status = pool.status()
        self.assertIsNone(status["generationTokensPerSecond"])
        self.assertNotIn("generationTokensPerSecond", status["loadedModels"][0])

    async def test_unload_one_model_leaves_the_other_resident(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        await pool.load(self.model(2))
        result = await pool.unload_model("model-1")
        self.assertEqual(result["count"], 1)
        self.assertNotIn("model-1", pool._slots)
        self.assertIn("model-2", pool._slots)

    async def test_unload_one_model_refuses_while_leased(self):
        pool = self.make_pool()
        await pool.load(self.model(1))
        w1 = pool._slots["model-1"].worker
        w1.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        t1 = asyncio.create_task(anext(g1))
        await asyncio.sleep(0.02)
        with self.assertRaises(MLXBarError) as raised:
            await pool.unload_model("model-1")
        self.assertEqual(raised.exception.code, "ENGINE_BUSY")
        w1.gen_release.set()
        await t1
        await g1.aclose()

    async def test_client_disconnect_while_queued_leaves_no_leaked_lane(self):
        pool = self.make_pool(concurrency=1)
        await pool.load(self.model(1))
        await pool.load(self.model(2))
        w1 = pool._slots["model-1"].worker
        w1.gen_release = asyncio.Event()
        g1 = pool.generate_for_model("model-1", "a", [], {}, "r1")
        t1 = asyncio.create_task(anext(g1))
        await asyncio.sleep(0.02)
        # r2 for a different model is queued behind the single permit.
        g2 = pool.generate_for_model("model-2", "b", [], {}, "r2")
        t2 = asyncio.create_task(anext(g2))
        await asyncio.sleep(0.02)
        self.assertIn("r2", pool._slots["model-2"].gen_queued)
        # Client goes away: close the generator while it is still waiting.
        t2.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t2
        await g2.aclose()
        await asyncio.sleep(0.02)
        self.assertEqual(pool._slots["model-2"].gen_queued, {})
        self.assertIsNone(pool._slots["model-2"].gen_owner)
        self.assertFalse(pool._slots["model-2"].gen_lock.locked())
        # The permit r1 holds is still the only one taken.
        self.assertEqual(pool._gen_active_lanes, 1)
        self.assertEqual(pool._gen_slots._value, 0)
        w1.gen_release.set()
        await t1
        await g1.aclose()
        self.assertEqual(pool._gen_active_lanes, 0)
        self.assertEqual(pool._gen_slots._value, 1)

    async def test_stress_concurrent_requests_leave_no_leaked_lane_state(self):
        """Hammer 3 models with overlapping + cancelled requests; nothing sticks."""
        self.settings.data["generation"]["maxQueuedRequests"] = 64
        pool = self.make_pool(concurrency=2)
        for number in (1, 2, 3):
            await pool.load(self.model(number))

        async def one(model_id: str, index: int, cancel: bool) -> None:
            gen = pool.generate_for_model(model_id, f"p{index}", [], {}, f"req-{index}")
            try:
                async for _event in gen:
                    if cancel:
                        break
            finally:
                await gen.aclose()

        tasks = []
        for index in range(24):
            model_id = f"model-{(index % 3) + 1}"
            tasks.append(asyncio.create_task(one(model_id, index, cancel=(index % 4 == 0))))
            if index % 5 == 0:
                await asyncio.sleep(0)  # interleave starts
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

        # Every lane fully released: no permit leak, semaphore back to full,
        # no owner or queue entry stranded, no lease left held.
        self.assertEqual(pool._gen_active_lanes, 0)
        self.assertEqual(pool._gen_slots._value, 2)
        for slot in pool._slots.values():
            self.assertFalse(slot.gen_lock.locked())
            self.assertIsNone(slot.gen_owner)
            self.assertFalse(slot.gen_permit)
            self.assertEqual(slot.gen_queued, {})
            self.assertEqual(slot.leases, 0)
        self.assertEqual(pool._request_workers, {})

    async def test_concurrency_never_exceeds_the_configured_limit_under_load(self):
        pool = self.make_pool(concurrency=2)
        for number in (1, 2, 3):
            await pool.load(self.model(number))
            pool._slots[f"model-{number}"].worker.gen_release = asyncio.Event()
        seen_peak = 0

        async def run(number: int) -> None:
            nonlocal seen_peak
            gen = pool.generate_for_model(f"model-{number}", "p", [], {}, f"r{number}")
            try:
                async for _event in gen:
                    seen_peak = max(seen_peak, pool._gen_active_lanes)
            finally:
                await gen.aclose()

        tasks = [asyncio.create_task(run(number)) for number in (1, 2, 3)]
        await asyncio.sleep(0.05)
        # Only two lanes may be active at once; the third request is queued.
        self.assertEqual(pool._gen_active_lanes, 2)
        self.assertEqual(sum(len(s.gen_queued) for s in pool._slots.values()), 1)
        for slot in pool._slots.values():
            slot.worker.gen_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        self.assertLessEqual(seen_peak, 2)
        self.assertEqual(pool._gen_active_lanes, 0)
