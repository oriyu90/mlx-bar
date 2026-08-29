"""v1.8.0 same-model replicas (`models.pool.profiles[].replicas`).

Guards the invariants: `replicas == 1` is byte-identical to v1.7.x; a replica is
an independent worker process; admission charges N times the memory; the pool
routes concurrent same-model generations to distinct replicas but never exceeds
`generationConcurrency`; `enabled=false` ignores replicas entirely.
"""

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


class ReplicaWorker:
    instances: list["ReplicaWorker"] = []

    def __init__(self, root, settings, *, instance_key="legacy", memory_limit_bytes=0, **kwargs):
        self.instance_key = instance_key
        self.memory_limit_bytes = memory_limit_bytes
        self.loaded = None
        self.loading = None
        self.engine = None
        self.active_requests: dict = {}
        self.queued_requests: dict = {}
        self.maintenance_engines: set = set()
        self.process = None
        self.socket_path = None
        self.unloaded = False
        self.gen_release: asyncio.Event | None = None
        ReplicaWorker.instances.append(self)

    async def load(self, model, engine=None):
        self.loaded = {**model, "engine": engine or model.get("engine"),
                       "capabilities": {"modelMaxTokens": 4096,
                                        "memoryLimits": {"set_memory_limit": self.memory_limit_bytes}}}
        self.engine = self.loaded["engine"]
        return self.loaded

    async def unload(self):
        self.unloaded = True
        self.loaded = None
        return {"state": "unloaded"}

    async def shutdown(self):
        self.loaded = None

    async def _call(self, method, params, timeout=5):
        if method == "count_tokens":
            return {"input_tokens": 7}
        return {"memory": {"process_rss_bytes": 700 << 20, "active_bytes": 400 << 20, "cache_bytes": 0}}

    async def generate(self, prompt, images, options, request_id=None, image_root=None):
        self.active_requests[request_id] = object()
        try:
            if self.gen_release is not None:
                await self.gen_release.wait()
            await asyncio.sleep(0)
            yield {"type": "delta", "text": f"{self.instance_key}:{prompt}"}
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


class ReplicaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 6, "maxReplicasPerModel": 4,
            "totalMemoryRatio": 0.75, "minimumSystemReserveGB": 1,
            "defaultPerModelMaxGB": 8, "idleTTLSeconds": 30, "generationConcurrency": 2,
        })
        ReplicaWorker.instances = []
        self.patch = patch("mlxbar.workers.model_pool.SingleWorkerSupervisor", ReplicaWorker)
        self.patch.start()
        self.pool = ModelPoolSupervisor(self.root, self.settings)
        self.pool._physical_memory = lambda: 64 * GIB
        self.pool._host_capacity = lambda: (48 * GIB, 1)

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.patch.stop()
        self.tmp.cleanup()

    def model(self, number, size_gb=1):
        return {"id": f"model-{number}", "name": f"model-{number}",
                "engine": "mlx-lm", "size_bytes": int(size_gb * GIB)}

    def _pin(self, model_id, replicas):
        self.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": model_id, "keepLoaded": True, "replicas": replicas}]

    async def test_default_replicas_is_one_and_keeps_the_bare_slot_key(self):
        await self.pool.load(self.model(1))
        self.assertEqual(list(self.pool._slots), ["model-1"])
        self.assertEqual(len(self.pool._replica_slots("model-1")), 1)
        self.assertEqual(self.pool._slots["model-1"].replica_index, 0)

    async def test_pinned_replicas_load_independent_workers(self):
        self._pin("model-1", 3)
        await self.pool.load(self.model(1), pin=True)
        keys = sorted(k for k in self.pool._slots)
        self.assertEqual(keys, ["model-1", "model-1#1", "model-1#2"])
        workers = {self.pool._slots[k].worker for k in keys}
        self.assertEqual(len(workers), 3)
        instance_keys = sorted(w.instance_key for w in workers)
        digest = instance_keys[0].split("-")[0]
        self.assertEqual(instance_keys, [digest, f"{digest}-1", f"{digest}-2"])

    async def test_replica_count_is_clamped_to_max_replicas_per_model(self):
        self.settings.data["models"]["pool"]["maxReplicasPerModel"] = 2
        self._pin("model-1", 8)
        await self.pool.load(self.model(1), pin=True)
        self.assertEqual(len(self.pool._replica_slots("model-1")), 2)

    async def test_admission_charges_per_replica_and_rejects_over_budget(self):
        # 6 GB model -> ~8.6 GB estimate each (< 10 GB per-model limit), but two
        # copies (~17 GB) exceed the 15 GB global budget.
        self.settings.data["models"]["pool"]["defaultPerModelMaxGB"] = 10
        self.pool._global_budget = lambda: 15 * GIB
        self._pin("model-1", 2)
        await self.pool.load(self.model(1, size_gb=6), pin=True)
        # First replica admitted, second rejected -> the model is still usable.
        self.assertEqual(len(self.pool._replica_slots("model-1")), 1)

    async def test_concurrent_same_model_requests_use_distinct_replicas(self):
        self._pin("model-1", 2)
        await self.pool.load(self.model(1), pin=True)
        for slot in self.pool._replica_slots("model-1"):
            slot.worker.gen_release = asyncio.Event()

        async def run(rid):
            return [e async for e in self.pool.generate_for_model(
                "model-1", "hi", [], {"max_tokens": 8}, rid)]

        a = asyncio.create_task(run("r1"))
        b = asyncio.create_task(run("r2"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            busy = [s for s in self.pool._replica_slots("model-1") if s.worker.active_requests]
            if len(busy) == 2:
                break
        self.assertEqual(len({id(s.worker) for s in self.pool._replica_slots("model-1")
                              if s.worker.active_requests}), 2)
        for slot in self.pool._replica_slots("model-1"):
            slot.worker.gen_release.set()
        await asyncio.wait_for(asyncio.gather(a, b), timeout=3)
        self.assertEqual(self.pool._gen_active_lanes, 0)

    async def test_generation_concurrency_one_serialises_even_with_replicas(self):
        self.settings.data["models"]["pool"]["generationConcurrency"] = 1
        pool = ModelPoolSupervisor(self.root, self.settings)
        pool._physical_memory = lambda: 64 * GIB
        pool._host_capacity = lambda: (48 * GIB, 1)
        try:
            self._pin("model-1", 2)
            await pool.load(self.model(1), pin=True)
            for slot in pool._replica_slots("model-1"):
                slot.worker.gen_release = asyncio.Event()

            async def run(rid):
                return [e async for e in pool.generate_for_model(
                    "model-1", "hi", [], {"max_tokens": 8}, rid)]

            a = asyncio.create_task(run("r1"))
            b = asyncio.create_task(run("r2"))
            await asyncio.sleep(0.1)
            busy = [s for s in pool._replica_slots("model-1") if s.worker.active_requests]
            self.assertEqual(len(busy), 1)  # concurrency 1 -> one at a time
            for slot in pool._replica_slots("model-1"):
                slot.worker.gen_release.set()
            await asyncio.wait_for(asyncio.gather(a, b), timeout=3)
        finally:
            await pool.shutdown()

    async def test_unload_model_frees_every_replica(self):
        self._pin("model-1", 3)
        await self.pool.load(self.model(1), pin=True)
        result = await self.pool.unload_model("model-1")
        self.assertEqual(result["count"], 3)
        self.assertEqual(self.pool._replica_slots("model-1"), [])

    async def test_status_reports_replica_index_and_count(self):
        self._pin("model-1", 2)
        await self.pool.load(self.model(1), pin=True)
        rows = [m for m in self.pool.status()["loadedModels"] if m["id"] == "model-1"]
        self.assertEqual(sorted(r["replicaIndex"] for r in rows), [0, 1])
        self.assertTrue(all(r["replicaCount"] == 2 for r in rows))
        self.assertEqual(self.pool.status()["modelPool"]["maxReplicasPerModel"], 4)

    async def test_reaper_scales_replicas_down_when_config_lowered(self):
        self._pin("model-1", 3)
        await self.pool.load(self.model(1), pin=True)
        self.assertEqual(len(self.pool._replica_slots("model-1")), 3)
        self.settings.data["models"]["pool"]["profiles"][0]["replicas"] = 1
        await self.pool._reap_once()
        self.assertEqual(len(self.pool._replica_slots("model-1")), 1)
        self.assertEqual(self.pool._replica_slots("model-1")[0].replica_index, 0)

    async def test_pool_disabled_ignores_replicas(self):
        self.settings.data["models"]["pool"]["enabled"] = False
        pool = ModelPoolSupervisor(self.root, self.settings)
        try:
            self._pin("model-1", 3)
            legacy_calls = []

            async def fake_legacy_load(model, engine=None):
                legacy_calls.append(model["id"])
                pool._legacy.loaded = {**model, "engine": "mlx-lm"}
                return pool._legacy.loaded

            pool._legacy.load = fake_legacy_load
            await pool.load(self.model(1), pin=True)
            self.assertEqual(legacy_calls, ["model-1"])  # one worker, no replicas
            self.assertEqual(len(pool._slots), 0)
        finally:
            await pool.shutdown()


if __name__ == "__main__":
    unittest.main()
