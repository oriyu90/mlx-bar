"""v2.8.1 regression tests: generation lane integrity under races.

The v2.8.0 stress suite intermittently leaked a pool generation lane
(``_gen_active_lanes`` stuck at 1 with every ``gen_permit`` already False and
the ``_gen_slots`` semaphore one permit short). The observed replay showed an
in-flight ``_acquire_lane`` task outliving its abandoned parent: a lane freed
by orphan recovery no longer stopped the parked task, which then committed
owner/permit/counter for a request nobody owns anymore. These tests pin the
abandon-marker machinery introduced in v2.8.1 plus the two-observation
liveness grace that keeps status-poll recovery from killing a live request
that merely has not resumed yet.
"""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor
from mlxbar.workers.supervisor import QueuedRequest

from test_model_pool import FakeWorker


class LaneIntegrityTests(unittest.IsolatedAsyncioTestCase):

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

    def make_pool(self) -> ModelPoolSupervisor:
        pool = ModelPoolSupervisor(self.root, self.settings)
        pool._physical_memory = lambda: 32 * GIB
        pool._host_capacity = lambda: (24 * GIB, 1)
        self._pools.append(pool)
        return pool

    @staticmethod
    def _parked_capacity(pool: ModelPoolSupervisor):
        """Replace the headroom probe with one that parks the caller."""
        started = threading.Event()
        release = threading.Event()

        def capacity():
            started.set()
            release.wait()
            return (24 * GIB, 1)

        pool._host_capacity = capacity
        return started, release

    @staticmethod
    async def _wait_started(started: threading.Event) -> None:
        while not started.is_set():
            await asyncio.sleep(0.01)

    async def test_inflight_acquisition_aborts_after_lane_recovery(self):
        """Replay of the v2.8.0 stranded-lane bug.

        A lane freed by orphan recovery must abort an acquire task that is
        still parked inside the concurrency headroom probe -- before the fix
        the resumed task committed owner/permit/counter for a request nobody
        owned, leaving ``_gen_active_lanes`` > 0 with no live holder and one
        semaphore permit consumed forever.
        """
        pool = self.make_pool()
        await pool.load({"id": "model-1", "name": "model-1", "engine": "mlx-lm"})
        slot = pool._slots["model-1"]
        # One "other" lane active so the acquire path probes headroom.
        pool._gen_active_lanes = 1
        started, release = self._parked_capacity(pool)

        acquire = asyncio.create_task(pool._acquire_lane(slot, "r2"))
        await self._wait_started(started)
        self.assertTrue(slot.gen_lock.locked())
        self.assertEqual(slot.gen_owner, "r2")
        self.assertEqual(pool._gen_slots._value, 1)  # raw permit held in flight

        # The lane now looks like a vanished owner's lane (second consecutive
        # observation, matching the status-poll cadence).
        self.assertFalse(pool._recover_lane(slot, "status"))
        slot.orphan_pending_since = time.monotonic() - 1
        self.assertTrue(pool._recover_lane(slot, "status"))
        self.assertIsNone(slot.gen_owner)  # recovery freed the lane
        self.assertFalse(slot.gen_lock.locked())

        release.set()
        await acquire
        # The task must not have committed anything; its own raw permit is
        # returned exactly once and the abandon marker is consumed.
        self.assertEqual(pool._gen_slots._value, 2)
        self.assertEqual(pool._gen_active_lanes, 1)
        self.assertIsNone(slot.gen_owner)
        self.assertFalse(slot.gen_permit)
        self.assertFalse(slot.gen_lock.locked())
        self.assertEqual(slot.gen_recoveries, 1)
        self.assertEqual(pool._abandoned_lanes, set())
        self.assertEqual(pool._lane_tasks, {})

        # And the pool is still fully usable afterwards.
        pool._host_capacity = lambda: (24 * GIB, 1)
        await pool._acquire_lane(slot, "r3")
        self.assertEqual(slot.gen_owner, "r3")
        self.assertEqual(pool._gen_active_lanes, 2)
        self.assertTrue(pool._release_lane(slot, "r3"))
        self.assertEqual(pool._gen_active_lanes, 1)

    async def test_abandoned_parent_neutralises_a_late_acquire_task(self):
        """Simulated finalizer order: abandon marker, lane release, cancel.

        Even when the acquire task is parked inside the headroom probe while
        its parent finalizes, the marker must stop it from committing.
        """
        pool = self.make_pool()
        await pool.load({"id": "model-1", "name": "model-1", "engine": "mlx-lm"})
        slot = pool._slots["model-1"]
        pool._gen_active_lanes = 1
        started, release = self._parked_capacity(pool)

        acquire = asyncio.create_task(pool._acquire_lane(slot, "r2"))
        await self._wait_started(started)
        self.assertEqual(slot.gen_owner, "r2")

        # Finalizer sequence (synchronous part).
        pool._abandoned_lanes.add("r2")
        self.assertTrue(pool._release_lane(slot, "r2"))
        self.assertIsNone(slot.gen_owner)
        acquire.cancel()
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await acquire

        self.assertEqual(pool._gen_slots._value, 2)
        self.assertEqual(pool._gen_active_lanes, 1)
        self.assertFalse(slot.gen_lock.locked())
        self.assertEqual(pool._abandoned_lanes, set())
        # The task registered itself; nothing may linger after cancellation.
        self.assertEqual(pool._lane_tasks, {})

    async def test_lane_recovery_requires_a_persisted_orphan_state(self):
        """A single orphan-like observation must not free a live lane.

        Between the lane commit and the worker-request registration a live
        request looks like a vanished owner for one event-loop step; the grace
        horizon is what keeps a status poll from killing it.
        """
        pool = self.make_pool()
        await pool.load({"id": "model-1", "name": "model-1", "engine": "mlx-lm"})
        slot = pool._slots["model-1"]
        await slot.gen_lock.acquire()
        slot.gen_owner = "r1"
        slot.gen_permit = True
        pool._gen_active_lanes = 1

        self.assertFalse(pool._recover_lane(slot, "status"))
        self.assertTrue(slot.gen_lock.locked())
        self.assertEqual(slot.gen_owner, "r1")
        self.assertEqual(slot.gen_recoveries, 0)
        # A live owner visible again clears the pending verdict, and the next
        # observation may then recover safely.
        slot.gen_queued["r1"] = QueuedRequest(asyncio.Event(), time.monotonic())
        self.assertFalse(pool._recover_lane(slot, "status"))
        self.assertIsNone(slot.orphan_pending_owner)
        slot.gen_queued.clear()
        # Two further observations (as successive status polls would produce)
        # now recover the lane.
        self.assertFalse(pool._recover_lane(slot, "status"))
        slot.orphan_pending_since = time.monotonic() - 1
        self.assertTrue(pool._recover_lane(slot, "status"))
        self.assertFalse(slot.gen_lock.locked())


if __name__ == "__main__":
    unittest.main()
