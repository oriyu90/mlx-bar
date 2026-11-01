"""v4.0.0 (WP-3, WP-5, WP-6): pool concurrency and future health.

Pins four behaviours that were broken before this change:

* WP-3a (FIND-05): when the leader of a singleflight load is cancelled, its
  cancellation must not be inherited by the waiters. A waiter re-enters the
  load path (bounded to LOAD_CANCELLED_REENTRY_LIMIT) and can succeed.
* WP-3b (FIND-06): the load future is resolved before any await in the failure
  path, so a `worker.shutdown()` that raises cannot leave waiters hanging.
* WP-5 (FIND-10): an unload that cannot take `_load_lock` within the bound is
  refused with 409 ENGINE_BUSY instead of blocking for the whole load.
* WP-6 (FIND-09/14): admission re-reads `keepLoaded` from settings, so a model
  unpinned just now can be chosen as an eviction victim.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_model_pool import FakeWorker

from mlxbar.errors import MLXBarError
from mlxbar.settings import SettingsStore
from mlxbar.workers import model_pool as model_pool_module
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor, PoolSlot


def model(name: str, size_gb: float) -> dict:
    return {"id": name, "name": name, "engine": "mlx-lm",
            "size_bytes": int(size_gb * GIB)}


class PoolConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 0, "defaultPerModelMaxGB": 32,
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
        self.pool._physical_memory = lambda: 96 * GIB
        self.pool._host_capacity = lambda: (60 * GIB, 0)

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    # WP-3a -----------------------------------------------------------------
    async def test_leader_cancel_does_not_propagate_to_waiter(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        target = model("alpha", 4)

        leader = asyncio.create_task(self.pool.load(target, None, pin=True))
        for _ in range(100):
            await asyncio.sleep(0)
            if FakeWorker.load_calls >= 1 and self.pool._slots:
                break
        self.assertEqual(FakeWorker.load_calls, 1, "the leader never started loading")

        waiter = asyncio.create_task(self.pool.load(target, None, pin=True))
        # Enough ticks for the waiter to attach to the *same* future. The proof
        # it attached (rather than starting a load of its own) is that the
        # worker's load() was still called exactly once -- a waiter that became
        # its own leader would have bumped this counter.
        for _ in range(100):
            await asyncio.sleep(0)
        self.assertFalse(waiter.done(), "the waiter finished before the cancel")
        self.assertEqual(FakeWorker.load_calls, 1,
                         "the waiter did not join the in-flight load")

        leader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await leader  # the leader itself is cancelled, as it should be

        # The waiter must NOT inherit that cancellation: it re-enters as the new
        # leader and returns a loaded model.
        FakeWorker.load_gate = None
        gate.set()
        result = await asyncio.wait_for(waiter, 5)
        self.assertEqual(result.get("id"), "alpha")
        self.assertFalse(waiter.cancelled())
        self.assertEqual(FakeWorker.load_calls, 2,
                         "the waiter did not re-enter the load after the cancel")

    async def test_waiter_reentry_is_bounded(self):
        # Force every attempt to resolve as LOAD_CANCELLED: the waiter must give
        # up after LOAD_CANCELLED_REENTRY_LIMIT re-entries instead of looping.
        target = model("beta", 4)
        error = MLXBarError("LOAD_CANCELLED", "cancelled", 503, True)
        loop = asyncio.get_running_loop()
        dead = loop.create_future()
        dead.set_exception(error)
        dead.exception()  # mark retrieved so the test does not warn

        slot_key = self.pool._slot_key("beta", 0)
        stub = PoolSlot(model=dict(target), worker=FakeWorker(self.root, self.settings),
                        reservation_bytes=0, load_future=dead, state="loading")
        self.pool._slots[slot_key] = stub

        with self.assertRaises(MLXBarError) as caught:
            await asyncio.wait_for(self.pool.load(target, None, pin=True), 5)
        self.assertEqual(caught.exception.code, "LOAD_CANCELLED")

    # WP-3b -----------------------------------------------------------------
    async def test_future_resolves_even_when_shutdown_raises(self):
        target = model("gamma", 4)

        async def failing_shutdown(self_worker=None):
            raise RuntimeError("shutdown exploded")

        # Load fails (cancelled leader) and the cleanup shutdown also raises.
        gate = asyncio.Event()
        FakeWorker.load_gate = gate

        async def boom_load(self_worker, model, engine=None, explicit=None):
            await gate.wait()
            raise asyncio.CancelledError()

        with patch.object(FakeWorker, "load", boom_load), \
                patch.object(FakeWorker, "shutdown", failing_shutdown):
            leader = asyncio.create_task(self.pool.load(target, None, pin=True))
            slot = None
            for _ in range(200):  # the slot is registered inside _admit
                await asyncio.sleep(0)
                slot = self.pool._slots.get(self.pool._slot_key("gamma", 0))
                if slot is not None:
                    break
            self.assertIsNotNone(slot)
            future = slot.load_future
            gate.set()
            with self.assertRaises((asyncio.CancelledError, RuntimeError)):
                await asyncio.wait_for(leader, 5)

        # Whatever the shutdown did, the future must be resolved, not pending.
        self.assertTrue(future.done(), "load future left pending after failure")

    # WP-5 ------------------------------------------------------------------
    async def test_unload_during_load_returns_engine_busy_quickly(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        target = model("delta", 4)
        loader = asyncio.create_task(self.pool.load(target, None, pin=True))
        await asyncio.sleep(0)  # the load now holds _load_lock

        with patch.object(model_pool_module, "LOAD_LOCK_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(MLXBarError) as caught:
                await asyncio.wait_for(self.pool.unload(), 1)
        self.assertEqual(caught.exception.code, "ENGINE_BUSY")
        self.assertEqual(caught.exception.status, 409)

        FakeWorker.load_gate = None
        gate.set()
        await asyncio.wait_for(loader, 5)

    # WP-6 ------------------------------------------------------------------
    async def test_unpinned_slot_is_a_victim_after_settings_change(self):
        # keepLoaded was true when the slot was created, then the user unpinned
        # it in settings. The slot still carries keep_loaded=True until admission
        # refreshes it; admission must now treat it as an evictable victim.
        self.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "held", "keepLoaded": False}]
        slot = PoolSlot(model=model("held", 20), worker=FakeWorker(self.root, self.settings),
                        reservation_bytes=20 * GIB, state="ready", keep_loaded=True,
                        last_released_at=0.0)
        self.pool._slots[self.pool._slot_key("held", 0)] = slot

        # Budget is 0.75 * 96 = 72 GiB. Pretend the resident charge is full so a
        # 60 GiB request needs the 20 GiB slot reclaimed.
        with patch.object(self.pool, "_resident_charge", return_value=20 * GIB), \
                patch.object(self.pool, "_global_budget", return_value=72 * GIB), \
                patch.object(self.pool, "_cold_estimate", return_value=60 * GIB), \
                patch.object(self.pool, "_per_model_limit", return_value=64 * GIB), \
                patch.object(self.pool, "_anonymous_estimate", return_value=GIB), \
                patch.object(self.pool, "_host_capacity", return_value=(60 * GIB, 0)):
            _, victims = await asyncio.wait_for(
                self.pool._admit(model("new", 60), resident=True), 5)

        self.assertIn(slot, victims)
        self.assertFalse(slot.keep_loaded)

    async def test_keep_loaded_slot_is_never_a_victim(self):
        self.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "held", "keepLoaded": True}]
        slot = PoolSlot(model=model("held", 20), worker=FakeWorker(self.root, self.settings),
                        reservation_bytes=20 * GIB, state="ready", keep_loaded=False,
                        last_released_at=0.0)
        self.pool._slots[self.pool._slot_key("held", 0)] = slot

        with patch.object(self.pool, "_resident_charge", return_value=20 * GIB), \
                patch.object(self.pool, "_global_budget", return_value=72 * GIB), \
                patch.object(self.pool, "_cold_estimate", return_value=60 * GIB), \
                patch.object(self.pool, "_per_model_limit", return_value=64 * GIB), \
                patch.object(self.pool, "_anonymous_estimate", return_value=GIB), \
                patch.object(self.pool, "_host_capacity", return_value=(60 * GIB, 0)):
            with self.assertRaises(MLXBarError):
                await asyncio.wait_for(
                    self.pool._admit(model("new", 60), resident=True), 5)
        self.assertTrue(slot.keep_loaded)


if __name__ == "__main__":
    unittest.main()
