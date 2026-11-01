"""v4.0.1 / WP-2 + WP-3: what must still work **while a model is loading**.

The v4.0.0 field report (TASK-008 / F-01) could not produce a load window at
all -- the memory-pressure gate refused every load -- so the "load in progress"
cases were never measured. `Tests/load_window.py` (WP-1) makes such a window
cheap to hold open, and these tests pin the two properties v4.0.0 claimed:

* WP-2 (F-09 good behaviour): a settings write does **not** queue behind a load.
  `PUT /settings/models/pool/profiles/{id}` and `PUT /settings` take only
  `settings.write_lock`; the load path never takes it. Writes must answer 200
  within 5 s and be persisted, even mid-load.
* WP-3 (F-02): a prompt-cache clear must not be routed to a slot whose worker
  is still loading. It returns immediately with `{"skipped": ...}` instead of
  blocking until the load finishes (or killing it).
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from load_window import load_in_progress
from test_model_pool import FakeWorker

from mlxbar.database import Database
from mlxbar.main import make_management_app
from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor

# The whole point of WP-2/WP-3 is "this answers now". Both budgets are far
# below the 5 s settings-lock timeout and far above any scheduling jitter.
IMMEDIATE_BUDGET_SECONDS = 1.0
SETTINGS_BUDGET_SECONDS = 5.0


def model(name: str, size_gb: float = 1) -> dict:
    return {"id": name, "name": name, "engine": "mlx-lm",
            "size_bytes": int(size_gb * GIB)}


class _LoadWindowBase(unittest.IsolatedAsyncioTestCase):
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
        self.database = Database(self.root / "state.sqlite3")
        # `/status`, `/settings` and the profile route read these attributes off
        # the app state directly (same stand-in as test_v2_8_overflow_idle).
        self.state = SimpleNamespace(
            settings=self.settings, workers=self.pool, database=self.database,
            model_autoload_lock=asyncio.Lock(), public_listener_error=None,
            listener=None)

    async def asyncTearDown(self):
        FakeWorker.load_gate = None
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.database.close()
        self.temporary.cleanup()

    @staticmethod
    def _forbid_cache_calls(worker) -> list:
        """Record prompt-cache RPCs so "did not touch the worker" is provable."""
        calls: list[str] = []

        async def memory():
            calls.append("memory")
            return {"enabled": True}

        async def disk():
            calls.append("disk")
            return {"enabled": True}

        async def paged():
            calls.append("paged")
            return {"enabled": True}

        worker.clear_memory_prompt_cache = memory
        worker.clear_disk_prompt_cache = disk
        worker.clear_paged_prompt_cache = paged
        return calls


class SettingsWriteDuringLoadTests(_LoadWindowBase):
    """WP-2: the settings surface must not couple to an in-flight load."""

    async def test_profile_write_is_fast_and_persisted_while_a_model_loads(self):
        transport = httpx.ASGITransport(app=make_management_app(self.state))
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            async with load_in_progress(self.pool, model("m1")) as window:
                self.assertEqual(window.slot().state, "loading")

                started = time.monotonic()
                response = await asyncio.wait_for(
                    client.put("/api/v1/settings/models/pool/profiles/m1",
                               json={"maxMemoryGB": 8}),
                    timeout=SETTINGS_BUDGET_SECONDS)
                elapsed = time.monotonic() - started

                self.assertEqual(response.status_code, 200, response.text)
                self.assertLess(elapsed, SETTINGS_BUDGET_SECONDS)
                self.assertEqual(window.slot().state, "loading",
                                 "the load must still be in flight during the write")

            # The write reached disk, not just memory.
            reloaded = SettingsStore(self.root)
            profiles = reloaded.data["models"]["pool"]["profiles"]
            self.assertEqual([item["modelId"] for item in profiles], ["m1"])
            self.assertEqual(profiles[0]["maxMemoryGB"], 8)

        # ... and the load it overlapped still completed.
        self.assertEqual(self.pool._slots["m1"].state, "ready")
        self.assertEqual([item["id"] for item in self.pool.loaded_models()], ["m1"])

    async def test_pool_settings_write_is_fast_and_persisted_while_a_model_loads(self):
        transport = httpx.ASGITransport(app=make_management_app(self.state))
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            async with load_in_progress(self.pool, model("m1")) as window:
                self.assertEqual(window.slot().state, "loading")

                started = time.monotonic()
                response = await asyncio.wait_for(
                    client.put("/api/v1/settings",
                               json={"models": {"pool": {"maxResidentModels": 4}}}),
                    timeout=SETTINGS_BUDGET_SECONDS)
                elapsed = time.monotonic() - started

                self.assertEqual(response.status_code, 200, response.text)
                self.assertLess(elapsed, SETTINGS_BUDGET_SECONDS)
                self.assertEqual(response.json()["models"]["pool"]["maxResidentModels"], 4)
                self.assertEqual(window.slot().state, "loading")

            reloaded = SettingsStore(self.root)
            self.assertEqual(
                reloaded.data["models"]["pool"]["maxResidentModels"], 4,
                "maxResidentModels must be persisted while a load is in flight")

        self.assertEqual(self.pool._slots["m1"].state, "ready")


class PromptCacheClearDuringLoadTests(_LoadWindowBase):
    """WP-3: a clear must skip a loading slot, return at once, and not kill it."""

    async def test_disk_clear_skips_the_loading_slot_and_returns_immediately(self):
        async with load_in_progress(self.pool, model("m1")) as window:
            calls = self._forbid_cache_calls(window.slot().worker)
            started = time.monotonic()
            result = await asyncio.wait_for(
                self.pool.clear_disk_prompt_cache(), timeout=IMMEDIATE_BUDGET_SECONDS)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, IMMEDIATE_BUDGET_SECONDS)
            self.assertEqual(result, {"skipped": "loading", "modelId": "m1"})
            self.assertEqual(calls, [], "no cache RPC may reach a loading worker")
            self.assertEqual(window.slot().state, "loading")

        # The load finished on its own: skipping a clear must never break it.
        self.assertEqual(self.pool._slots["m1"].state, "ready")
        self.assertFalse(self.pool._slots["m1"].worker.unloaded)

    async def test_memory_clear_skips_a_loading_primary_slot(self):
        async with load_in_progress(self.pool, model("m1")) as window:
            # The pool can be primary-set while the only replica is still
            # loading (a reloaded primary, a replica swap). That is exactly the
            # state `_primary_slot()` returns a non-ready slot in.
            self.pool._primary_model_id = "m1"
            calls = self._forbid_cache_calls(window.slot().worker)
            started = time.monotonic()
            result = await asyncio.wait_for(
                self.pool.clear_memory_prompt_cache(), timeout=IMMEDIATE_BUDGET_SECONDS)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, IMMEDIATE_BUDGET_SECONDS)
            self.assertEqual(result, {"skipped": "loading", "modelId": "m1"})
            self.assertEqual(calls, [])

        self.assertEqual(self.pool._slots["m1"].state, "ready")

    async def test_paged_clear_skips_the_loading_slot_too(self):
        # Same class of RPC, same hazard; kept in step on purpose.
        async with load_in_progress(self.pool, model("m1")) as window:
            calls = self._forbid_cache_calls(window.slot().worker)
            result = await asyncio.wait_for(
                self.pool.clear_paged_prompt_cache(), timeout=IMMEDIATE_BUDGET_SECONDS)
            self.assertEqual(result, {"skipped": "loading", "modelId": "m1"})
            self.assertEqual(calls, [])

    async def test_clears_work_normally_once_the_load_is_ready(self):
        # The guard must not turn into "clears never work".
        async with load_in_progress(self.pool, model("m1")):
            pass
        slot = self.pool._slots["m1"]
        self.assertEqual(slot.state, "ready")
        self.assertEqual(await self.pool.clear_disk_prompt_cache(), {"enabled": False})
        self.assertEqual(await self.pool.clear_memory_prompt_cache(), {"enabled": False})

    async def test_the_legacy_path_skips_while_the_single_worker_loads(self):
        # Pool disabled: the coordinator still must not queue a clear behind a
        # load in the one worker it has.
        self.settings.data["models"]["pool"]["enabled"] = False
        legacy = ModelPoolSupervisor(self.root, self.settings)
        try:
            legacy._legacy.loading = {"id": "legacy-model"}
            self.assertEqual(await legacy.clear_memory_prompt_cache(),
                             {"skipped": "loading", "modelId": "legacy-model"})
            self.assertEqual(await legacy.clear_disk_prompt_cache(),
                             {"skipped": "loading", "modelId": "legacy-model"})
        finally:
            await legacy.shutdown()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
