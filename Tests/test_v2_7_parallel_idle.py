"""v2.7.0 idle-state + on-demand parallel replica + TTL visibility.

Covers the three user-visible fixes without touching existing suites:

1. ``status()`` self-heals a stranded lane/lease (client disconnect without
   ``aclose``) so the menu bar returns to "idle" within one poll interval,
   and never disturbs a live generation.
2. A 2nd concurrent same-model generation grows one best-effort on-demand
   replica (within ``maxReplicasPerModel`` and ``generationConcurrency``,
   still gated by the memory head-room guard) instead of queueing behind the
   1st -- even with ``maxResidentModels == 1``, which only caps *distinct*
   model ids. Memory refusal falls back to queueing (pre-v2.7.0 behaviour).
3. The non-resident unload timing (``idleTTLSeconds``) is visible from
   ``status()`` (``modelPool.idleTTLSeconds`` + per-model ``idleExpiresAt``)
   and applies live without a restart.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


class BlockingWorker:
    """Fake pool worker whose generations block until the test releases them."""

    instances: list["BlockingWorker"] = []
    block: asyncio.Event | None = None
    load_calls = 0
    observed_bytes = 768 << 20

    def __init__(self, root, settings, **kwargs):
        self.root = root
        self.settings = settings
        self.loaded = None
        self.loading = None
        self.engine = None
        self.active_requests: dict = {}
        self.queued_requests: dict = {}
        self.maintenance_engines: set = set()
        self.memory_limit_bytes = kwargs.get("memory_limit_bytes", 0)
        self.process = None
        self.socket_path = None
        self.unloaded = False
        BlockingWorker.instances.append(self)

    @staticmethod
    def _is_our_worker(pid):
        return False

    async def load(self, model, engine=None, explicit=None):
        BlockingWorker.load_calls += 1
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
        return {"memory": {"process_rss_bytes": BlockingWorker.observed_bytes,
                           "active_bytes": 512 << 20, "cache_bytes": 0}}

    async def generate(self, prompt, images, options, request_id=None, image_root=None):
        self.active_requests[request_id] = object()
        try:
            if BlockingWorker.block is not None:
                await BlockingWorker.block.wait()
            await asyncio.sleep(0)
            yield {"type": "delta", "text": str(prompt)}
            yield {"type": "completed", "finish_reason": "stop"}
        finally:
            self.active_requests.pop(request_id, None)

    async def cancel(self, request_id):
        return {"cancelled": request_id in self.active_requests}

    async def prompt_cache_stats(self):
        return {"enabled": False}

    async def clear_memory_prompt_cache(self):
        return {"enabled": False}

    async def clear_disk_prompt_cache(self):
        return {"enabled": False}

    async def wait_until_idle(self, timeout=30):
        return True

    async def probe_runtime(self, engine):
        return {"ok": True, "engine": engine}

    def begin_maintenance(self, engine):
        self.maintenance_engines.add(engine)

    def end_maintenance(self, engine):
        self.maintenance_engines.discard(engine)

    def effective_max_tokens(self):
        return 4096

    def effective_max_prompt_characters(self):
        return 16384

    def raise_if_queue_full(self):
        return None

    def status(self):
        return {"loadedModel": self.loaded, "loadingModel": self.loading,
                "activeRequestCount": len(self.active_requests),
                "queuedRequestCount": len(self.queued_requests),
                "workerRunning": self.loaded is not None}


def make_model(number, size_gb=1):
    return {"id": f"model-{number}", "name": f"model-{number}",
            "engine": "mlx-lm", "size_bytes": int(size_gb * GIB)}


class PoolTestBase(unittest.IsolatedAsyncioTestCase):
    pool_kwargs: dict = {}

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        pool = {"enabled": True, "maxResidentModels": 1,
                "totalMemoryRatio": 0.75, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "idleTTLSeconds": 30,
                "generationConcurrency": 2, "maxReplicasPerModel": 2}
        pool.update(self.pool_kwargs)
        self.settings.data["models"]["pool"].update(pool)
        BlockingWorker.instances = []
        BlockingWorker.load_calls = 0
        BlockingWorker.block = None
        BlockingWorker.observed_bytes = 768 << 20
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", BlockingWorker)
        self.worker_patch.start()
        self.pool = ModelPoolSupervisor(self.root, self.settings)
        self.pool._physical_memory = lambda: 32 * GIB
        self.pool._host_capacity = lambda: (24 * GIB, 1)

    async def asyncTearDown(self):
        BlockingWorker.block = None
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    async def _collect(self, generation, out: list):
        try:
            async for event in generation:
                out.append(event)
        finally:
            await generation.aclose()


class StatusRecoveryTests(PoolTestBase):
    async def test_status_heals_a_stranded_lane_and_lease(self):
        await self.pool.load_for_api(make_model(1))
        slot = self.pool._slots["model-1"]
        # Simulate a disconnect that never closed the generator: no live
        # request anywhere, but bookkeeping left behind.
        slot.leases = 1
        slot.gen_owner = "ghost-request"
        self.assertEqual(slot.worker.active_requests, {})
        self.assertEqual(slot.gen_queued, {})
        status = self.pool.status()
        self.assertEqual(slot.leases, 0)
        self.assertIsNone(slot.gen_owner)
        entry = next(item for item in status["loadedModels"] if item["id"] == "model-1")
        self.assertEqual(entry["activeLeases"], 0)
        self.assertEqual(entry["laneQueueDepth"], 0)
        self.assertGreaterEqual(status["modelPool"]["activeGenerations"], 0)

    async def test_status_leaves_a_live_generation_alone(self):
        await self.pool.load_for_api(make_model(1))
        BlockingWorker.block = asyncio.Event()
        try:
            events: list = []
            generation = self.pool.generate_for_model("model-1", "hi", [], {}, "live-1")
            task = asyncio.create_task(self._collect(generation, events))
            for _ in range(100):
                slot = self.pool._slots["model-1"]
                if slot.worker.active_requests:
                    break
                await asyncio.sleep(0.02)
            slot = self.pool._slots["model-1"]
            self.assertTrue(slot.worker.active_requests)
            before = slot.gen_recoveries
            status = self.pool.status()
            # Still reported as busy, with no spurious recovery counted.
            self.assertEqual(slot.leases, 1)
            self.assertEqual(slot.gen_owner, "live-1")
            self.assertEqual(slot.gen_recoveries, before)
            entry = next(item for item in status["loadedModels"] if item["id"] == "model-1")
            self.assertEqual(entry["activeLeases"], 1)
            BlockingWorker.block.set()
            await asyncio.wait_for(task, 10)
            self.assertTrue(any(event.get("type") == "completed" for event in events))
            self.assertEqual(slot.leases, 0)
        finally:
            BlockingWorker.block = None


class OnDemandExpansionTests(PoolTestBase):
    async def test_second_parallel_generation_expands_despite_max_resident_one(self):
        # maxResidentModels == 1 must not block a same-model 2nd lane: it
        # caps distinct model ids, and the expansion is the same id.
        await self.pool.load_for_api(make_model(1))
        BlockingWorker.block = asyncio.Event()
        try:
            first_events: list = []
            second_events: list = []
            gen1 = self.pool.generate_for_model("model-1", "one", [], {}, "exp-1")
            task1 = asyncio.create_task(self._collect(gen1, first_events))
            for _ in range(100):
                if any(worker.active_requests for worker in BlockingWorker.instances):
                    break
                await asyncio.sleep(0.02)
            gen2 = self.pool.generate_for_model("model-1", "two", [], {}, "exp-2")
            task2 = asyncio.create_task(self._collect(gen2, second_events))
            for _ in range(200):
                actives = sum(len(worker.active_requests)
                              for worker in BlockingWorker.instances)
                if actives >= 2:
                    break
                await asyncio.sleep(0.02)
            # Both lanes run at once on two replicas.
            self.assertEqual(len(self.pool._replica_slots("model-1")), 2)
            actives = sum(len(worker.active_requests)
                          for worker in BlockingWorker.instances)
            self.assertGreaterEqual(actives, 2)
            BlockingWorker.block.set()
            await asyncio.wait_for(asyncio.gather(task1, task2), 10)
            for slot in self.pool._replica_slots("model-1"):
                self.assertEqual(slot.leases, 0)
        finally:
            BlockingWorker.block = None

    async def test_memory_refusal_falls_back_to_queueing(self):
        await self.pool.load_for_api(make_model(1))
        BlockingWorker.block = asyncio.Event()
        try:
            first_events: list = []
            gen1 = self.pool.generate_for_model("model-1", "one", [], {}, "fb-1")
            task1 = asyncio.create_task(self._collect(gen1, first_events))
            for _ in range(100):
                if any(worker.active_requests for worker in BlockingWorker.instances):
                    break
                await asyncio.sleep(0.02)
            # Memory pressure refuses the expansion load: the 2nd request
            # must queue behind the busy replica (pre-v2.7.0 behaviour).
            self.pool._host_capacity = lambda: (24 * GIB, 2)
            second_events: list = []
            gen2 = self.pool.generate_for_model("model-1", "two", [], {}, "fb-2")
            task2 = asyncio.create_task(self._collect(gen2, second_events))
            for _ in range(200):
                queued = any("fb-2" in slot.gen_queued
                             for slot in self.pool._replica_slots("model-1"))
                if queued:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(self.pool._replica_slots("model-1")), 1)
            self.assertTrue(any("fb-2" in slot.gen_queued
                                for slot in self.pool._replica_slots("model-1")))
            self.pool._host_capacity = lambda: (24 * GIB, 1)
            BlockingWorker.block.set()
            await asyncio.wait_for(asyncio.gather(task1, task2), 10)
        finally:
            BlockingWorker.block = None

    async def test_no_expansion_when_replicas_capped_at_one(self):
        self.settings.data["models"]["pool"]["maxReplicasPerModel"] = 1
        # The concurrency ceiling is latched at pool construction; rebuild it
        # so the latched semaphore matches the settings under test.
        await self.pool.shutdown()
        self.pool = ModelPoolSupervisor(self.root, self.settings)
        self.pool._physical_memory = lambda: 32 * GIB
        self.pool._host_capacity = lambda: (24 * GIB, 1)
        loads_before = BlockingWorker.load_calls
        await self.pool.load_for_api(make_model(1))
        self.assertEqual(BlockingWorker.load_calls, loads_before + 1)
        BlockingWorker.block = asyncio.Event()
        try:
            first_events: list = []
            gen1 = self.pool.generate_for_model("model-1", "one", [], {}, "cap-1")
            task1 = asyncio.create_task(self._collect(gen1, first_events))
            for _ in range(100):
                if any(worker.active_requests for worker in BlockingWorker.instances):
                    break
                await asyncio.sleep(0.02)
            second_events: list = []
            gen2 = self.pool.generate_for_model("model-1", "two", [], {}, "cap-2")
            task2 = asyncio.create_task(self._collect(gen2, second_events))
            for _ in range(200):
                queued = any("cap-2" in slot.gen_queued
                             for slot in self.pool._replica_slots("model-1"))
                if queued:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(self.pool._replica_slots("model-1")), 1)
            self.assertEqual(BlockingWorker.load_calls, loads_before + 1)
            self.assertTrue(any("cap-2" in slot.gen_queued
                                for slot in self.pool._replica_slots("model-1")))
            BlockingWorker.block.set()
            await asyncio.wait_for(asyncio.gather(task1, task2), 10)
        finally:
            BlockingWorker.block = None


class TTLVisibilityTests(PoolTestBase):
    pool_kwargs = {"maxResidentModels": 2}

    async def test_idle_expiry_is_visible_and_live(self):
        await self.pool.load_for_api(make_model(1))
        await self.pool.load(make_model(2), pin=True)
        status = self.pool.status()
        by_id = {item["id"]: item for item in status["loadedModels"]}
        self.assertEqual(status["modelPool"]["idleTTLSeconds"], 30)
        # Unpinned model carries an expiry; pinned never expires.
        self.assertIsInstance(by_id["model-1"]["idleExpiresAt"], float)
        self.assertGreater(by_id["model-1"]["idleExpiresAt"], time.time())
        self.assertIsNone(by_id["model-2"]["idleExpiresAt"])
        # Retuning the TTL needs no restart: status reflects it immediately.
        self.settings.data["models"]["pool"]["idleTTLSeconds"] = 60
        status = self.pool.status()
        self.assertEqual(status["modelPool"]["idleTTLSeconds"], 60)
