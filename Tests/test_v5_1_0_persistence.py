"""v5.1.0: a model must not persist by itself after an API request.

The report: "the model keeps getting persisted by itself when model requests
happen". The cause was that *every* load path that passed ``pin=True`` -- and
the background replica top-up passed the slot's own pin state back in -- made a
model session-pinned, which the idle TTL never touches. These tests pin the
rule that replaces it: only an explicit load (GUI/CLI, or the startup preload
of a configured resident) may pin; an API autoload, the reaper and an on-demand
replica are always transient.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlxbar.settings import SettingsStore
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor


class _Worker:
    instances: list["_Worker"] = []

    def __init__(self, root, settings, *, instance_key="legacy", memory_limit_bytes=0, **kwargs):
        self.instance_key = instance_key
        self.memory_limit_bytes = memory_limit_bytes
        self.loaded = None
        self.active_requests: dict = {}
        self.queued_requests: dict = {}
        self.maintenance_engines: set = set()
        self.process = None
        self.socket_path = None
        self.load_calls = 0
        _Worker.instances.append(self)

    async def load(self, model, engine=None, explicit=None, **kwargs):
        self.load_calls += 1
        self.loaded = {**model, "engine": engine or model.get("engine"),
                       "capabilities": {"modelMaxTokens": 4096,
                                        "memoryLimits": {"set_memory_limit": self.memory_limit_bytes}}}
        return self.loaded

    async def unload(self):
        self.loaded = None
        return {"state": "unloaded"}

    async def shutdown(self):
        self.loaded = None

    async def _call(self, method, params, timeout=5):
        return {"memory": {"process_rss_bytes": 500 << 20, "active_bytes": 300 << 20,
                           "cache_bytes": 0}}

    async def generate(self, prompt, images, options, request_id=None, image_root=None):
        yield {"type": "delta", "text": "x"}
        yield {"type": "completed", "finish_reason": "stop"}

    async def cancel(self, request_id):
        return {"cancelled": False}

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
        return {"loadedModel": self.loaded, "loadingModel": None,
                "activeRequestCount": len(self.active_requests),
                "queuedRequestCount": len(self.queued_requests),
                "workerRunning": self.loaded is not None}


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = SettingsStore(self.root)
        self.settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 4, "maxReplicasPerModel": 4,
            "totalMemoryRatio": 0.9, "minimumSystemReserveGB": 1,
            "defaultPerModelMaxGB": 8, "idleTTLSeconds": 30, "generationConcurrency": 2,
        })
        _Worker.instances = []
        self.patch = patch("mlxbar.workers.model_pool.SingleWorkerSupervisor", _Worker)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def make_pool(self) -> ModelPoolSupervisor:
        pool = ModelPoolSupervisor(self.root, self.settings)
        pool._physical_memory = lambda: 32 * GIB
        pool._host_capacity = lambda: (24 * GIB, 1)
        self.addAsyncCleanup(pool.shutdown)
        return pool

    @staticmethod
    def model(number=1):
        return {"id": f"model-{number}", "name": f"model-{number}",
                "engine": "mlx-lm", "size_bytes": GIB}

    async def test_api_autoload_is_never_pinned(self):
        pool = self.make_pool()
        await pool.load_for_api(self.model(1))
        slot = pool._slots["model-1"]
        self.assertFalse(slot.session_pinned)
        self.assertIsNotNone(pool._idle_remaining(slot))

    async def test_api_autoload_of_a_pinned_model_keeps_the_pin(self):
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        self.assertTrue(slot.session_pinned)
        await pool.load_for_api(self.model(1))
        # Re-loading a model the user pinned must not *unpin* it either.
        self.assertTrue(slot.session_pinned)

    async def test_manual_load_pins(self):
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        self.assertTrue(pool._slots["model-1"].session_pinned)

    async def test_origin_wins_over_a_stale_pin_argument(self):
        """The reaper's old call shape (pin=<current pin>) can no longer pin."""
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        slot.session_pinned = False  # simulate a released pin
        await pool.load(self.model(1), pin=True, origin="reaper")
        self.assertFalse(slot.session_pinned)

    async def test_reaper_does_not_top_up_a_session_pinned_model(self):
        """One manual load is one copy -- not a standing request for replicas."""
        self.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "model-1", "keepLoaded": False, "replicas": 3}]
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        before = len(_Worker.instances)
        await pool._scale_up_pinned_replicas()
        self.assertEqual(len(_Worker.instances), before)
        self.assertEqual(pool._slots["model-1"].session_pinned, True)

    async def test_reaper_still_tops_up_a_keep_loaded_profile(self):
        self.settings.data["models"]["pool"]["profiles"] = [
            {"modelId": "model-1", "keepLoaded": True, "replicas": 2}]
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="preload")
        # The explicit load already brought both replicas up; the reaper must
        # not add a third.
        self.assertEqual(len(pool._replica_slots("model-1")), 2)
        await pool._scale_up_pinned_replicas()
        self.assertEqual(len(pool._replica_slots("model-1")), 2)

    async def test_api_autoload_does_not_become_resident_over_the_cap(self):
        self.settings.data["models"]["pool"]["maxResidentModels"] = 1
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        await pool.load_for_api(self.model(2))
        second = pool._slots["model-2"]
        self.assertFalse(second.session_pinned)
        # It may be admitted as a non-resident (v2.8.0), never promoted.
        self.assertFalse(second.resident)

    async def test_transient_model_expires_on_the_idle_ttl(self):
        pool = self.make_pool()
        await pool.load_for_api(self.model(1))
        slot = pool._slots["model-1"]
        slot.last_released_at -= 10_000  # far past idleTTLSeconds
        victims = await pool._reap_once()
        self.assertGreaterEqual(victims, 1)
        self.assertNotIn("model-1", pool._slots)

    async def test_pinned_model_survives_the_reaper(self):
        pool = self.make_pool()
        await pool.load(self.model(1), pin=True, origin="manual")
        slot = pool._slots["model-1"]
        slot.last_released_at -= 10_000
        await pool._reap_once()
        self.assertIn("model-1", pool._slots)


class ExplicitLoadOriginTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_origin_is_transient(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = SettingsStore(Path(tmp.name))
        settings.data["models"]["pool"].update({
            "enabled": True, "maxResidentModels": 4, "totalMemoryRatio": 0.9,
            "minimumSystemReserveGB": 1, "defaultPerModelMaxGB": 8})
        _Worker.instances = []
        with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor", _Worker):
            pool = ModelPoolSupervisor(Path(tmp.name), settings)
            pool._physical_memory = lambda: 32 * GIB
            pool._host_capacity = lambda: (24 * GIB, 1)
            try:
                await pool.load({"id": "m", "name": "m", "engine": "mlx-lm",
                                 "size_bytes": GIB}, pin=True, origin="something-new")
                self.assertFalse(pool._slots["m"].session_pinned)
            finally:
                await pool.shutdown()


if __name__ == "__main__":
    unittest.main()


class StartupRestoreTests(unittest.IsolatedAsyncioTestCase):
    """v5.1.0: what the next start restores, and what it must not.

    The preload reads `last_loaded_model_id`. An API request used to write it,
    so one request made a model come back on every restart; it now writes
    `last_api_model_id` instead, which nothing restores.
    """

    class _Database:
        def __init__(self, values=None):
            self.values = dict(values or {})

        def metadata_value(self, key):
            return self.values.get(key)

        def set_metadata_value(self, key, value):
            self.values[key] = value

        def list_models(self):
            return [{"id": "model-1", "name": "model-1", "engine": "mlx-lm"},
                    {"id": "model-2", "name": "model-2", "engine": "mlx-lm"}]

    class _Workers:
        def __init__(self):
            self.loaded_ids = []

        async def load(self, model, pin=True, **kwargs):
            self.loaded_ids.append((model["id"], pin, kwargs.get("origin")))
            return {"id": model["id"]}

        def status(self):
            return {"loadedModels": []}

    def _state(self, database, workers, *, preload_last=True, profiles=()):
        from types import SimpleNamespace
        return SimpleNamespace(
            settings=SimpleNamespace(data={
                "models": {"pool": {"profiles": list(profiles)}},
                "general": {"preloadLastModel": preload_last}}),
            database=database,
            workers=workers,
            model_autoload_lock=asyncio.Lock(),
            preload_state={"state": "pending", "modelId": None, "reason": None,
                           "code": None, "at": None},
            set_preload_state=lambda value, **kwargs: None,
        )

    async def test_api_autoload_alone_restores_nothing(self):
        from mlxbar import main as main_module
        database = self._Database({"last_api_model_id": "model-1",
                                   "api_autoload_suspended": "0"})
        workers = self._Workers()
        state = self._state(database, workers)
        task = main_module._preload_last_model(state, retry_interval=0, retry_attempts=0)
        if task is not None:
            await task
        self.assertEqual(workers.loaded_ids, [])

    async def test_explicit_load_is_restored_transiently(self):
        from mlxbar import main as main_module
        database = self._Database({"last_loaded_model_id": "model-2",
                                   "api_autoload_suspended": "0"})
        workers = self._Workers()
        state = self._state(database, workers)
        task = main_module._preload_last_model(state, retry_interval=0, retry_attempts=0)
        await task
        # Restored without a pin: the user asked to keep using this model, not
        # to make it permanent, so the idle TTL still applies to it.
        self.assertEqual([(model, pin) for model, pin, _ in workers.loaded_ids],
                         [("model-2", False)])
        self.assertEqual(workers.loaded_ids[0][2], "preload-transient")

    async def test_keep_loaded_profile_is_restored_pinned(self):
        from mlxbar import main as main_module
        database = self._Database({"api_autoload_suspended": "0"})
        workers = self._Workers()
        state = self._state(database, workers,
                            profiles=[{"modelId": "model-1", "keepLoaded": True}])
        task = main_module._preload_last_model(state, retry_interval=0, retry_attempts=0)
        await task
        self.assertEqual([(model, pin, origin) for model, pin, origin in workers.loaded_ids],
                         [("model-1", True, "preload")])
