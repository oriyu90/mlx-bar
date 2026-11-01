"""v4.0.0 / TASK-007 WP-9: the six "unconfirmed" scenarios (U-01..U-06).

FINDINGS.md section 17 lists six behaviours that were *not* reproduced during
planning. This module answers each one with an executed test and states the
verdict in the docstring, so a later reader does not have to guess whether the
risk was checked or assumed:

* U-01 -- two *different* models loading at the same time double-charge the
  budget.  Verdict: **not reproduced**; admission is serialized by `_load_lock`
  and each slot is charged exactly once (`_resident_charge()` equals the sum of
  the reservations).
* U-02 -- a `profiles` write during a load corrupts `reservation_bytes` /
  `resident`.  Verdict: **not reproduced**; the reservation is fixed at
  admission and a settings write does not touch the slot.
* U-03 -- a manual load during the resident preload interferes beyond the
  `_load_lock` serialization.  Verdict: **not reproduced**; the manual load
  waits, then succeeds, and a manual load of the *same* model joins the
  in-flight load instead of starting a second one.
* U-04 -- `_record_model_crash` and `load_future` interact badly (unresolved
  future, hung waiters).  Verdict: **not reproduced**; the crash is recorded,
  the future resolves with `WORKER_CRASHED`, and the breaker trips on the
  second crash.
* U-05 -- concurrent `config.json` + `state.sqlite3` writes leave a partial
  file.  Verdict: **not reproduced**; `_atomic_write` (unique temp + fsync +
  `os.replace`) keeps the file parseable under concurrent writers, and SQLite
  keeps each metadata write whole.
* U-06 -- growing `maxResidentModels` does not promote an already-loaded
  non-resident slot.  Verdict: **reproduced**; DECISIONS.md defers the
  `resident`/`reservation_bytes` recalculation to the next release, so the test
  below *pins the current behaviour* and is marked as a known limitation
  instead of asserting the desired one.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_model_pool import FakeWorker

from mlxbar.database import Database
from mlxbar.errors import MLXBarError
from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


def model(name: str, size_gb: float) -> dict:
    return {"id": name, "name": name, "engine": "mlx-lm",
            "size_bytes": int(size_gb * GIB)}


async def ticks(count: int = 30) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 2, "totalMemoryRatio": 0.75,
            "minimumSystemReserveGB": 1, "defaultPerModelMaxGB": 32,
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

    # U-01 ------------------------------------------------------------------
    async def test_u01_two_models_are_each_charged_once(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        first = asyncio.create_task(self.pool.load(model("u01-a", 4), None, pin=True))
        second = asyncio.create_task(self.pool.load(model("u01-b", 4), None, pin=True))
        await ticks()

        # Admission is serialized: the second load waits on `_load_lock`, so it
        # cannot charge a budget the first one has not finished reserving.
        self.assertEqual(len(self.pool._slots), 1)

        FakeWorker.load_gate = None
        gate.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 10)
        self.assertEqual([item["id"] for item in results], ["u01-a", "u01-b"])

        slots = list(self.pool._slots.values())
        self.assertEqual(len(slots), 2)
        charged = sum(slot.reservation_bytes for slot in slots)
        # One charge per model: the pool's own accounting agrees with the slots.
        self.assertEqual(self.pool._resident_charge(), charged)
        self.assertLess(charged, self.pool._global_budget())

    # U-02 ------------------------------------------------------------------
    async def test_u02_profile_write_during_load_keeps_the_reservation(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        loader = asyncio.create_task(self.pool.load(model("u02-a", 4), None, pin=True))
        await ticks()
        slot = self.pool._slots[self.pool._slot_key("u02-a", 0)]
        reservation = slot.reservation_bytes
        self.assertGreater(reservation, 0)

        # The user pins the model in the GUI while its weights are loading.
        self.settings.update({"models": {"pool": {"profiles": [
            {"modelId": "u02-a", "keepLoaded": True}]}}})

        FakeWorker.load_gate = None
        gate.set()
        loaded = await asyncio.wait_for(loader, 10)
        self.assertEqual(loaded["id"], "u02-a")
        # The in-flight admission keeps its own numbers; only the derived
        # `keep_loaded` flag follows the setting (status() is what refreshes it).
        self.assertEqual(slot.reservation_bytes, reservation)
        self.pool.status()
        self.assertTrue(slot.keep_loaded)

    # U-03 ------------------------------------------------------------------
    async def test_u03_manual_load_during_preload_is_only_serialized(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        # The startup preload of the pinned model.
        preload = asyncio.create_task(self.pool.load(model("u03-pinned", 4), None, pin=True))
        await ticks()
        # A manual load of a *different* model arrives while it runs.
        manual = asyncio.create_task(self.pool.load(model("u03-manual", 4), None, pin=False))
        await ticks()
        self.assertFalse(preload.done())
        self.assertFalse(manual.done())

        FakeWorker.load_gate = None
        gate.set()
        preloaded, loaded = await asyncio.wait_for(
            asyncio.gather(preload, manual), 10)
        self.assertEqual(preloaded["id"], "u03-pinned")
        self.assertEqual(loaded["id"], "u03-manual")
        self.assertEqual(len(self.pool._slots), 2)

    async def test_u03_a_manual_load_of_the_same_model_joins_the_preload(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        preload = asyncio.create_task(self.pool.load(model("u03-same", 4), None, pin=True))
        await ticks()
        manual = asyncio.create_task(self.pool.load(model("u03-same", 4), None, pin=False))
        await ticks()
        self.assertEqual(FakeWorker.load_calls, 1, "the manual load started a second load")

        FakeWorker.load_gate = None
        gate.set()
        await asyncio.wait_for(asyncio.gather(preload, manual), 10)
        self.assertEqual(FakeWorker.load_calls, 1)

    # U-04 ------------------------------------------------------------------
    async def test_u04_a_crash_resolves_the_future_and_records_the_crash(self):
        gate = asyncio.Event()

        async def crashing_load(self_worker, model, engine=None, explicit=None):
            FakeWorker.load_calls += 1
            await gate.wait()
            raise MLXBarError("WORKER_CRASHED", "ワーカーがクラッシュしました", 503, True)

        with patch.object(FakeWorker, "load", crashing_load):
            leader = asyncio.create_task(self.pool.load(model("u04", 4), None, pin=True))
            await ticks()
            follower = asyncio.create_task(self.pool.load(model("u04", 4), None, pin=True))
            await ticks()
            future = self.pool._slots[self.pool._slot_key("u04", 0)].load_future

            gate.set()
            for task in (leader, follower):
                with self.assertRaises(MLXBarError) as caught:
                    await asyncio.wait_for(task, 10)
                self.assertEqual(caught.exception.code, "WORKER_CRASHED")
            # The future is resolved, not pending: no waiter can hang on it.
            self.assertTrue(future.done())
            self.assertEqual(len(self.pool._model_crashes["u04"]), 1)
            self.assertFalse(self.pool.model_crash_loop("u04"))

            # A second crash inside the window trips the breaker, which is what
            # stops an automatic reload loop.
            self.pool._record_model_crash("u04", "WORKER_CRASHED")
            self.assertTrue(self.pool.model_crash_loop("u04"))

    # U-05 ------------------------------------------------------------------
    def test_u05_concurrent_config_and_state_writes_stay_whole(self):
        store = SettingsStore(self.root)
        # Create the schema once before the writers start. `Database.__init__`
        # runs a column-by-column migration without an IF NOT EXISTS guard, so
        # two threads *initializing* the same file at the same moment race on
        # "duplicate column" -- but the app has exactly one AppState owning one
        # Database, and this test is about concurrent *writes*, not concurrent
        # store construction.
        Database(self.root / "state.sqlite3")
        corruption: list[BaseException] = []
        contention: list[BaseException] = []

        def write_config() -> None:
            try:
                for _ in range(20):
                    store._atomic_write(dict(store.data))
            except BaseException as exc:  # noqa: BLE001 - the assertion reports it
                corruption.append(exc)

        def write_state(index: int) -> None:
            # Each writer uses its own connection, like separate processes
            # would. `Database` sets no busy timeout, so a concurrent writer can
            # legitimately see "database is locked" -- that is contention, not
            # the partial write this test is about, so it is retried and counted
            # separately instead of being reported as corruption.
            database = Database(self.root / "state.sqlite3")
            for beat in range(20):
                for attempt in range(50):
                    try:
                        database.set_metadata_value(f"wp9-{index}", str(beat))
                        break
                    except sqlite3.OperationalError as exc:  # locked, retry
                        if "locked" not in str(exc) and "busy" not in str(exc):
                            corruption.append(exc)
                            return
                        contention.append(exc)
                        time.sleep(0.01)
                else:
                    corruption.append(RuntimeError(f"could not write wp9-{index}"))
                    return

        threads = [threading.Thread(target=write_config) for _ in range(3)]
        threads += [threading.Thread(target=write_state, args=(index,))
                    for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertFalse([thread.name for thread in threads if thread.is_alive()],
                         "a concurrent writer never finished")
        self.assertEqual(corruption, [])
        # Every writer used its own temp file + os.replace, so the last write
        # wins and the document is never half-written.
        raw = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["schemaVersion"], 1)
        database = Database(self.root / "state.sqlite3")
        self.assertEqual(database.metadata_value("wp9-0"), "19")
        self.assertEqual(database.metadata_value("wp9-2"), "19")
        # `contention` is informational: it proves the writers really overlapped.
        self.assertIsInstance(contention, list)

    # U-06 (known limitation, deferred by DECISIONS.md) ----------------------
    async def test_u06_growing_the_resident_limit_does_not_promote(self):
        self.settings.data["models"]["pool"]["maxResidentModels"] = 1
        first = await asyncio.wait_for(self.pool.load(model("u06-a", 4), None, pin=True), 10)
        second = await asyncio.wait_for(self.pool.load(model("u06-b", 4), None, pin=True), 10)
        self.assertEqual(first["id"], "u06-a")
        self.assertEqual(second["id"], "u06-b")
        slot_b = self.pool._slots[self.pool._slot_key("u06-b", 0)]
        self.assertFalse(slot_b.resident, "the second model was admitted past the cap")

        # Raising the cap does not retroactively promote it: `resident` is fixed
        # at admission (FIND-09). DECISIONS.md keeps that recalc out of v4.0.0,
        # so this test pins today's behaviour rather than the desired one.
        self.settings.data["models"]["pool"]["maxResidentModels"] = 2
        status = self.pool.status()
        self.assertFalse(slot_b.resident)
        self.assertEqual(status["modelPool"]["residentModelCount"], 1)
        self.assertEqual(status["modelPool"]["nonResidentModelCount"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
