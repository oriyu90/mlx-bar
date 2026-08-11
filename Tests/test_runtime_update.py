from __future__ import annotations

import unittest
import asyncio
import json
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

from mlxbar.runtimes.service import RuntimeUpdateService
from mlxbar.runtimes.slots import SlotStore
from mlxbar.runtimes.updater import RuntimeUpdater
from mlxbar.database import Database
from mlxbar.jobs import JobManager
from mlxbar.errors import MLXBarError


class FakeUpdater:
    def __init__(self, available=True):
        self.available = available
        self.staged = 0

    async def check(self, engine):
        return {"engine": engine, "currentVersion": "1.0", "candidateVersion": "2.0",
                "updateAvailable": self.available}

    async def stage(self, engine, progress, version=None):
        self.staged += 1
        await progress(0.8, "検証中")
        return {"slotId": "new-slot", "probe": {"compatible": True, "version": version},
                "manifest": {"package": f"{engine}=={version}"}}


class FakeSlots:
    def __init__(self):
        self.current = "old-slot"
        self.restored = None

    def active(self, _engine):
        return {"active": self.current, "previous": None}

    def activate(self, _engine, slot):
        self.current = slot
        return {"active": slot, "previous": "old-slot"}

    def restore(self, _engine, slot):
        self.current = slot
        self.restored = slot
        return {"active": slot, "previous": None}

    def cleanup(self, _engine, _keep=3):
        return []


class FakeWorkers:
    def __init__(self, fail_probe=False, loaded=None, idle=True):
        self.fail_probe = fail_probe
        self.loaded = loaded
        self.idle = idle
        self.probed = []
        self.reloaded = []
        self.maintenance = set()

    async def unload(self):
        self.loaded = None

    async def probe_runtime(self, engine):
        self.probed.append(engine)
        if self.fail_probe:
            raise RuntimeError("health failed")
        return {"ok": True}

    async def load(self, model, engine):
        self.loaded = {**model, "engine": engine}
        self.reloaded.append(engine)
        return self.loaded

    async def wait_until_idle(self, timeout=30):
        return self.idle

    def begin_maintenance(self, engine):
        self.maintenance.add(engine)

    def end_maintenance(self, engine):
        self.maintenance.discard(engine)


class FakeDatabase:
    def __init__(self):
        self.history = []

    def add_runtime_history(self, engine, slot, action, result):
        self.history.append((engine, slot, action, result))


class RuntimeUpdateServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []

    async def progress(self, value, message):
        self.events.append((value, message))

    async def test_one_click_update_stages_activates_and_probes(self):
        updater, slots, workers, database = FakeUpdater(), FakeSlots(), FakeWorkers(), FakeDatabase()
        service = RuntimeUpdateService(updater, slots, workers, database)
        result = await service.update_latest("mlx-lm", self.progress)
        self.assertTrue(result["updated"])
        self.assertEqual(slots.current, "new-slot")
        self.assertEqual(workers.probed, ["mlx-lm"])
        self.assertEqual([item[2] for item in database.history], ["staged", "activated"])

    async def test_failed_post_switch_probe_restores_previous_slot(self):
        updater, slots = FakeUpdater(), FakeSlots()
        workers, database = FakeWorkers(fail_probe=True), FakeDatabase()
        service = RuntimeUpdateService(updater, slots, workers, database)
        with self.assertRaisesRegex(Exception, "health failed"):
            await service.update_latest("mlx-vlm", self.progress)
        self.assertEqual(slots.current, "old-slot")
        self.assertEqual(slots.restored, "old-slot")
        self.assertEqual(database.history[-1][2], "failed")
        self.assertTrue(database.history[-1][3]["rolledBack"])

    async def test_already_latest_does_not_create_slot(self):
        updater = FakeUpdater(available=False)
        service = RuntimeUpdateService(updater, FakeSlots(), FakeWorkers(), FakeDatabase())
        result = await service.update_latest("mlx-lm", self.progress)
        self.assertFalse(result["updated"])
        self.assertEqual(updater.staged, 0)

    async def test_loaded_model_is_reloaded_after_successful_update(self):
        model = {"id": "model", "engine": "mlx-lm", "name": "test"}
        workers = FakeWorkers(loaded=model)
        slots, database = FakeSlots(), FakeDatabase()
        result = await RuntimeUpdateService(FakeUpdater(), slots, workers, database).update_latest("mlx-lm", self.progress)
        self.assertTrue(result["updated"])
        self.assertEqual(workers.reloaded, ["mlx-lm"])
        self.assertEqual(workers.loaded["id"], "model")

    async def test_active_generation_prevents_switch(self):
        model = {"id": "model", "engine": "mlx-vlm", "name": "test"}
        workers = FakeWorkers(loaded=model, idle=False)
        slots, database = FakeSlots(), FakeDatabase()
        with self.assertRaisesRegex(Exception, "生成が続いている"):
            await RuntimeUpdateService(FakeUpdater(), slots, workers, database).update_latest("mlx-vlm", self.progress)
        self.assertEqual(slots.current, "old-slot")


class SlotCleanupTests(unittest.TestCase):
    def test_cleanup_keeps_active_previous_and_latest_spare(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStore(Path(directory))
            root = store.engine_root("mlx-lm") / "slots"
            for slot_id in ("001", "002", "003", "004"):
                slot = root / slot_id
                slot.mkdir()
                (slot / "probe.json").write_text(json.dumps({"compatible": True, "version": slot_id}))
            store.activate("mlx-lm", "003")
            store.activate("mlx-lm", "004")
            removed = store.cleanup("mlx-lm", keep=3)
            self.assertEqual(removed, ["001"])
            self.assertTrue((root / "003").exists())
            self.assertTrue((root / "004").exists())

    def test_delete_previous_slot_clears_rollback_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStore(Path(directory))
            root = store.engine_root("mlx-vlm") / "slots"
            for slot_id in ("old", "current"):
                slot = root / slot_id
                slot.mkdir()
                (slot / "probe.json").write_text(json.dumps({"compatible": True, "version": slot_id}))
            store.activate("mlx-vlm", "old")
            store.activate("mlx-vlm", "current")
            result = store.delete("mlx-vlm", "old")
            self.assertTrue(result["removedPrevious"])
            self.assertFalse((root / "old").exists())
            self.assertEqual(store.active("mlx-vlm"), {"active": "current", "previous": None})

    def test_delete_active_slot_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStore(Path(directory))
            slot = store.engine_root("mlx-lm") / "slots" / "current"
            slot.mkdir()
            (slot / "probe.json").write_text(json.dumps({"compatible": True, "version": "1.0"}))
            store.activate("mlx-lm", "current")
            with self.assertRaisesRegex(ValueError, "使用中"):
                store.delete("mlx-lm", "current")


class RuntimeProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_command_sends_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            updater = RuntimeUpdater(SlotStore(Path(directory)))
            ticks = []

            async def heartbeat(elapsed):
                ticks.append(elapsed)

            await updater._command(sys.executable, "-c", "import time; time.sleep(1.2)", heartbeat=heartbeat)
            self.assertIn(1, ticks)

    async def test_long_command_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            updater = RuntimeUpdater(SlotStore(Path(directory)))
            task = asyncio.create_task(updater._command(
                sys.executable, "-c", "import time; time.sleep(30)"
            ))
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_job_manager_records_user_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(Database(Path(directory) / "state.sqlite3"))

            async def work(update):
                await update(0.28, "ダウンロード中")
                await asyncio.sleep(30)

            job = manager.create("runtime_stage:mlx-vlm", work)
            await asyncio.sleep(0.05)
            cancelled = await manager.cancel(job["id"])
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertEqual(cancelled["error"]["code"], "JOB_CANCELLED")


class RuntimeVersionCheckTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, current: str | None, candidate: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            store = SlotStore(Path(directory))
            if current:
                slot = store.engine_root("mlx-lm") / "slots" / "current"
                slot.mkdir()
                (slot / "probe.json").write_text(json.dumps({"compatible": True, "version": current}))
                store.activate("mlx-lm", "current")

            class Response:
                def raise_for_status(self): pass
                def json(self):
                    return {"info": {"version": candidate, "requires_python": ">=3.10",
                                     "package_url": "https://pypi.org/project/mlx-lm/"}}

            class Client:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
                async def get(self, _url): return Response()

            with patch("mlxbar.runtimes.updater.httpx.AsyncClient", return_value=Client()):
                return await RuntimeUpdater(store).check("mlx-lm")

    async def test_equal_normalized_version_is_latest(self):
        result = await self.check("1.2.0", "1.2.0")
        self.assertFalse(result["updateAvailable"])
        self.assertEqual(result["versionStatus"], "latest")

    async def test_older_version_has_update(self):
        result = await self.check("1.9", "1.10")
        self.assertTrue(result["updateAvailable"])
        self.assertEqual(result["versionStatus"], "update_available")

    async def test_newer_local_version_is_not_downgraded(self):
        result = await self.check("2.0rc1", "1.9")
        self.assertFalse(result["updateAvailable"])
        self.assertEqual(result["versionStatus"], "newer_than_stable")

    async def test_missing_runtime_is_installable(self):
        result = await self.check(None, "1.0")
        self.assertTrue(result["updateAvailable"])
        self.assertEqual(result["versionStatus"], "not_installed")

    async def test_invalid_remote_version_is_reported(self):
        with self.assertRaises(MLXBarError):
            await self.check("1.0", "not a version")


class RuntimeJobRecoveryTests(unittest.TestCase):
    def test_incomplete_runtime_job_is_marked_failed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.sqlite3")
            database.upsert_job({"id": "job", "kind": "runtime_update:mlx-lm", "state": "running",
                                 "progress": 0.28, "message": "ダウンロード中", "result": None, "error": None})
            database.fail_incomplete_jobs()
            job = database.get_job("job")
            self.assertEqual(job["state"], "failed")
            self.assertEqual(job["error"]["code"], "JOB_INTERRUPTED")


if __name__ == "__main__":
    unittest.main()
