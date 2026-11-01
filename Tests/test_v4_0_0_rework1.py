"""v4.0.0 REWORK #1 — 独立レビュー（TO-REVIEW）の指摘 R-01 / R-02 / R-03。

`tasks/TASK-007/REVIEW/REVIEW_REPORT.md` の HIGH/MEDIUM を固定する。

* R-01 (HIGH): シャットダウン中に、キャンセルされた leader の waiter が再入して
  新しい slot / worker を作ってしまう。`shutdown()` は slot をスナップショットして
  片付けるので、その後に生まれた worker は孤児になる。
* R-02 (MEDIUM): 設定ロックの待ち時間切れが `PUT /settings` では 500 になり、
  同じ状況の `PUT /settings/models/pool/profiles/{id}` は 409 を返していた。
* R-03 (MEDIUM): `worker.shutdown()` が例外を投げると slot が残り、そのモデルは
  再起動まで読み込めなくなる。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_model_pool import FakeWorker

from mlxbar.api import management as management_module
from mlxbar.database import Database
from mlxbar.errors import MLXBarError
from mlxbar.main import make_management_app
from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


def model(name: str, size_gb: float = 4) -> dict:
    return {"id": name, "name": name, "engine": "mlx-lm",
            "size_bytes": int(size_gb * GIB)}


class _ExplodingWorker(FakeWorker):
    """load() fails, and so does shutdown() — the R-03 combination."""

    async def load(self, model, engine=None, explicit=None):
        FakeWorker.load_calls += 1
        raise MLXBarError("WORKER_CRASHED", "boom", 503, False)

    async def shutdown(self):
        raise RuntimeError("shutdown failed")


class PoolShutdownTests(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self):
        FakeWorker.load_gate = None
        self.temporary.cleanup()

    def make_pool(self, worker_class=FakeWorker):
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", worker_class)
        self.worker_patch.start()
        pool = ModelPoolSupervisor(self.root, self.settings)
        pool._physical_memory = lambda: 96 * GIB
        pool._host_capacity = lambda: (60 * GIB, 0)
        return pool

    async def test_r01_a_waiter_does_not_start_a_load_while_shutting_down(self):
        gate = asyncio.Event()
        FakeWorker.load_gate = gate
        pool = self.make_pool()
        target = model("alpha")

        leader = asyncio.create_task(pool.load(target, None, pin=True))
        for _ in range(200):
            await asyncio.sleep(0)
            if FakeWorker.load_calls >= 1 and pool._slots:
                break
        self.assertEqual(FakeWorker.load_calls, 1, "the leader never started")

        waiter = asyncio.create_task(pool.load(target, None, pin=True))
        for _ in range(100):
            await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.assertEqual(FakeWorker.load_calls, 1, "the waiter did not join")

        # The leader goes away (client disconnect / coordinator shutdown) and the
        # coordinator starts tearing down in the same tick.
        leader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await leader
        await pool.shutdown()
        FakeWorker.load_gate = None
        gate.set()

        # R-01: the waiter must not become a fresh leader and spawn a worker that
        # shutdown() already passed. The refusal is specific: on the pre-fix code
        # it never gets here (it inherits the leader's CancelledError), and
        # without `_closing` it would re-enter and bump the load counter.
        with self.assertRaises(MLXBarError) as caught:
            await asyncio.wait_for(waiter, timeout=5)
        self.assertEqual(caught.exception.code, "ENGINE_SHUTTING_DOWN")
        self.assertEqual(FakeWorker.load_calls, 1,
                         "a waiter started a new load during shutdown")
        self.worker_patch.stop()

    async def test_r01_a_new_load_is_refused_once_shutdown_began(self):
        pool = self.make_pool()
        await pool.shutdown()
        with self.assertRaises(MLXBarError) as caught:
            await pool.load(model("alpha"), None, pin=True)
        self.assertEqual(caught.exception.code, "ENGINE_SHUTTING_DOWN")
        self.worker_patch.stop()

    async def test_r03_a_failing_shutdown_still_drops_the_slot(self):
        pool = self.make_pool(_ExplodingWorker)
        with self.assertRaises(MLXBarError) as caught:
            await pool.load(model("alpha"), None, pin=True)
        # The caller must see the load failure, not the teardown's RuntimeError.
        self.assertEqual(caught.exception.code, "WORKER_CRASHED")
        self.assertEqual(pool._slots, {},
                         "a stale slot survived a failing shutdown")
        with self.assertRaises(MLXBarError) as again:
            await pool.load(model("alpha"), None, pin=True)
        self.assertEqual(again.exception.code, "WORKER_CRASHED",
                         "the model could not be retried after a failed teardown")
        self.worker_patch.stop()


    async def test_r17_a_load_past_the_entry_gate_cannot_publish_after_shutdown(self):
        """REVIEW R-17: the entry check happens before `_admit` awaits I/O.

        A load that is already inside `_admit` when shutdown() runs must not
        create a slot: shutdown() has snapshotted the slots, so the worker would
        never be torn down.
        """
        pool = self.make_pool()
        gate = asyncio.Event()
        entered = asyncio.Event()
        original = pool._admit

        async def slow_admit(model_row, *, resident):
            entered.set()
            await gate.wait()
            return await original(model_row, resident=resident)

        pool._admit = slow_admit
        load = asyncio.create_task(pool.load(model("alpha"), None, pin=True))
        await asyncio.wait_for(entered.wait(), timeout=5)
        await pool.shutdown()
        gate.set()
        with self.assertRaises(MLXBarError) as caught:
            await asyncio.wait_for(load, timeout=5)
        self.assertEqual(caught.exception.code, "ENGINE_SHUTTING_DOWN")
        self.assertEqual(pool._slots, {}, "a slot was published after shutdown")
        self.worker_patch.stop()


class SettingsLockHttpTests(unittest.TestCase):
    """R-02: both settings endpoints answer 409, not 500, when the lock is held."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.database = Database(self.root / "state.sqlite3")

        async def unload_model(model_id, force=False):
            return {"state": "unloaded", "id": model_id}

        async def unload_all():
            return {"state": "unloaded"}

        self.workers = SimpleNamespace(unload_model=unload_model,
                                       unload=unload_all, _slots={})
        self.state = SimpleNamespace(settings=self.settings, database=self.database,
                                     workers=self.workers)
        self.client = TestClient(make_management_app(self.state))
        self.previous_timeout = management_module.SETTINGS_LOCK_TIMEOUT_SECONDS
        management_module.SETTINGS_LOCK_TIMEOUT_SECONDS = 0.05

    def tearDown(self):
        management_module.SETTINGS_LOCK_TIMEOUT_SECONDS = self.previous_timeout
        self.database.close()
        self.temporary.cleanup()

    def test_settings_put_times_out_with_409(self):
        async def hold():
            lock = self.settings.write_lock
            await lock.acquire()
            try:
                return self.client.put("/api/v1/settings",
                                       json={"general": {"preloadLastModel": False}})
            finally:
                lock.release()

        response = asyncio.run(hold())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "SETTINGS_CONFLICT")

    def test_the_profile_put_times_out_with_409_too(self):
        async def hold():
            lock = self.settings.write_lock
            await lock.acquire()
            try:
                return self.client.put(
                    "/api/v1/settings/models/pool/profiles/m1",
                    json={"keepLoaded": True})
            finally:
                lock.release()

        response = asyncio.run(hold())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "SETTINGS_CONFLICT")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
