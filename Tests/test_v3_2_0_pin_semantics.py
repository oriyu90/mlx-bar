"""v3.2.0 (ISS-3/ISS-7 / WP-310, WP-311, WP-710, WP-711, WP-712).

Two user-visible defects meet here:

* the "fixed" badge was drawn from `keepLoaded`, which is
  `slot.keep_loaded or slot.session_pinned`. A plain manual load pins the
  session, so a model the user never pinned still showed 📌 and "固定";
  un-pinning it removed nothing and the icon stayed on (AC-7-1).
* changing residency rewrote the whole `profiles` array from a GUI snapshot.
  Before `/settings` had been fetched that snapshot was empty, so pinning one
  model deleted every other residency setting (AC-7-4).

The API now reports `profilePinned` separately and offers a per-model merge
endpoint. Startup preload is scoped to pinned models only (AC-3-2), a single
unload no longer arms the global autoload gate (AC-3-4), and the preload
outcome is published in `status` (AC-3-3).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_model_pool import FakeWorker

from mlxbar.api.management import PRELOAD_STATES, _preload_status
from mlxbar.database import Database
from mlxbar.main import make_management_app
from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


def model(name: str, size_gb: float = 1) -> dict:
    return {"id": name, "name": name, "engine": "mlx-lm",
            "size_bytes": int(size_gb * GIB)}


class ProfilePinnedStatusTests(unittest.IsolatedAsyncioTestCase):
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

    def _entry(self, name: str) -> dict:
        return next(item for item in self.pool.status()["loadedModels"]
                    if item.get("id") == name)

    async def test_manual_load_is_kept_but_not_profile_pinned(self):
        await self.pool.load(model("manual"), pin=True)
        entry = self._entry("manual")
        self.assertTrue(entry["keepLoaded"])       # session-pinned: kept alive
        self.assertFalse(entry["profilePinned"])   # ...but not a saved pin

    async def test_saved_profile_sets_profile_pinned(self):
        self.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "wanted", "keepLoaded": True},
        ]
        await self.pool.load(model("wanted"), pin=True)
        entry = self._entry("wanted")
        self.assertTrue(entry["keepLoaded"])
        self.assertTrue(entry["profilePinned"])


class ProfileMergeEndpointTests(unittest.TestCase):
    """`PUT /api/v1/settings/models/pool/profiles/{model_id:path}` (WP-712)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.database = Database(self.root / "state.sqlite3")
        self.workers = SimpleNamespace(_slots={})
        self.state = SimpleNamespace(settings=self.settings,
                                     database=self.database,
                                     workers=self.workers)
        self.client = TestClient(make_management_app(self.state))

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def profiles(self) -> list[dict]:
        return self.settings.data["models"]["pool"]["profiles"]

    def put(self, model_id: str, body: dict):
        return self.client.put(
            f"/api/v1/settings/models/pool/profiles/{model_id}", json=body)

    def test_merge_keeps_the_other_profiles(self):
        self.assertEqual(self.put("org/model-a", {"keepLoaded": True}).status_code, 200)
        self.assertEqual(self.put("org/model-b", {"keepLoaded": True, "replicas": 2}).status_code, 200)
        # A later edit of A must not touch B (the old GUI rewrote the array).
        response = self.put("org/model-a", {"maxMemoryGB": 48})
        self.assertEqual(response.status_code, 200)
        profiles = {item["modelId"]: item for item in self.profiles()}
        self.assertEqual(set(profiles), {"org/model-a", "org/model-b"})
        self.assertTrue(profiles["org/model-a"]["keepLoaded"])
        self.assertEqual(profiles["org/model-a"]["maxMemoryGB"], 48)
        self.assertEqual(profiles["org/model-b"]["replicas"], 2)

    def test_replicas_and_memory_keep_the_pin(self):
        self.put("m", {"keepLoaded": True})
        self.put("m", {"keepLoaded": True, "replicas": 3})
        self.put("m", {"keepLoaded": True, "maxMemoryGB": 12})
        entry = self.profiles()[0]
        self.assertTrue(entry["keepLoaded"])
        self.assertEqual(entry["replicas"], 3)
        self.assertEqual(entry["maxMemoryGB"], 12)

    def test_unpin_removes_the_entry_and_forgets_last_loaded(self):
        self.put("m", {"keepLoaded": True})
        self.database.set_metadata_value("last_loaded_model_id", "m")
        response = self.put("m", {"keepLoaded": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.profiles(), [])
        self.assertNotEqual(self.database.metadata_value("last_loaded_model_id"), "m")
        # A single unpin must not arm the *global* autoload gate (AC-3-4).
        self.assertNotEqual(self.database.metadata_value("api_autoload_suspended"), "1")

    def test_invalid_replicas_is_a_422_with_a_code(self):
        response = self.put("m", {"replicas": 0})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "INVALID_PROFILE")

    def test_legacy_full_replacement_still_works(self):
        self.put("a", {"keepLoaded": True})
        response = self.client.put("/api/v1/settings", json={
            "models": {"pool": {"profiles": [{"modelId": "b", "keepLoaded": True}]}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["modelId"] for item in self.profiles()], ["b"])


class SingleUnloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root)
        self.database = Database(self.root / "state.sqlite3")
        self.gates: list[str] = []

        async def unload_model(model_id, force=False):
            return {"state": "unloaded", "id": model_id}

        async def unload_all():
            return {"state": "unloaded"}

        self.workers = SimpleNamespace(unload_model=unload_model,
                                       unload=unload_all, _slots={})
        self.state = SimpleNamespace(settings=self.settings,
                                     database=self.database,
                                     workers=self.workers)
        self.client = TestClient(make_management_app(self.state))

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def test_single_unload_clears_last_loaded_without_arming_the_gate(self):
        self.database.set_metadata_value("last_loaded_model_id", "m1")
        response = self.client.post("/api/v1/models/m1/unload")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.database.metadata_value("last_loaded_model_id"), "m1")
        self.assertNotEqual(self.database.metadata_value("api_autoload_suspended"), "1")

    def test_all_unload_keeps_the_global_gate(self):
        response = self.client.delete("/api/v1/models/loaded")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.metadata_value("api_autoload_suspended"), "1")


class PreloadStatusTests(unittest.TestCase):
    """AC-3-3: the startup preload outcome is always published, never guessed."""

    def test_five_keys_are_always_present(self):
        value = _preload_status(SimpleNamespace())
        self.assertEqual(set(value), {"state", "modelId", "reason", "code", "at"})
        self.assertEqual(value["state"], "pending")

    def test_unknown_state_falls_back_to_pending(self):
        value = _preload_status(SimpleNamespace(preload_state={"state": "exploded"}))
        self.assertEqual(value["state"], "pending")

    def test_failure_keeps_the_reason_and_code(self):
        value = _preload_status(SimpleNamespace(preload_state={
            "state": "failed", "modelId": "m", "reason": "空きメモリ不足",
            "code": "MEMORY_HEADROOM_INSUFFICIENT", "at": 123.0}))
        self.assertEqual(value["state"], "failed")
        self.assertEqual(value["code"], "MEMORY_HEADROOM_INSUFFICIENT")
        self.assertEqual(value["reason"], "空きメモリ不足")
        self.assertEqual(value["at"], 123.0)

    def test_every_published_state_is_in_the_allowed_set(self):
        for state in PRELOAD_STATES:
            self.assertEqual(_preload_status(
                SimpleNamespace(preload_state={"state": state}))["state"], state)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
