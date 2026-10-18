"""v2.8.0 non-resident models past the resident limit, and the per-model idle
extension.

Owner request: `maxResidentModels` is a cap on how many models hold a *resident*
slot. Once that many are loaded, other models must still be loadable -- they
simply do not count against the cap, and they are unloaded once their idle time
runs out. Both the GUI and `mlxbarctl` need to add N seconds to that remaining
time, and the remaining time itself has to be shown in seconds.

v2.7.3 refused the extra model with RESIDENT_LIMIT_REACHED instead; the refusal
and its tests are superseded (see test_v2_7_3_resident_limit.py). What must NOT
change, and is asserted here, is memory safety: a non-resident load is still
refused when the budget, the per-model cap or the OS pressure says no.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "Coordinator"))

import test_model_pool  # noqa: E402
from mlxbar.cli import execute  # noqa: E402
from mlxbar.database import Database  # noqa: E402
from mlxbar.errors import MLXBarError  # noqa: E402
from mlxbar.main import make_public_app  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402
from mlxbar.workers.model_pool import (  # noqa: E402
    MAX_IDLE_EXTENSION_SECONDS, GIB, ModelPoolSupervisor,
)


def _catalog(mid: str, name: str):
    return {"id": mid, "source": "filesystem", "name": name, "path": "/tmp/" + name,
            "provider_key": None, "format": "mlx", "engine": "mlx-lm",
            "modalities": ["text"], "confidence": 1.0, "reason": "test", "size_bytes": 1}


def _store(root: Path, max_n: int) -> SettingsStore:
    store = SettingsStore(root)
    store.data["api"]["requireToken"] = False
    store.data["models"]["pool"].update({
        "enabled": True, "maxResidentModels": max_n, "minimumSystemReserveGB": 1,
        "defaultPerModelMaxGB": 8, "generationConcurrency": 2, "idleTTLSeconds": 30,
    })
    return store


def _pool(root: Path, store: SettingsStore) -> ModelPoolSupervisor:
    pool = ModelPoolSupervisor(root, store)
    pool._physical_memory = lambda: 32 * GIB
    pool._host_capacity = lambda: (24 * GIB, 1)
    return pool


def _reset_fake() -> None:
    test_model_pool.FakeWorker.instances = []
    test_model_pool.FakeWorker.load_gate = None
    test_model_pool.FakeWorker.load_calls = 0
    test_model_pool.FakeWorker.observed_bytes = 768 << 20


class NonResidentAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = _store(self.root, 2)
        _reset_fake()
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", test_model_pool.FakeWorker)
        self.worker_patch.start()
        self.pool = _pool(self.root, self.settings)

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    async def test_below_the_limit_loads_a_resident(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        self.assertTrue(self.pool._slots["model-a"].resident)
        self.assertTrue(self.pool._slots["model-b"].resident)
        self.assertEqual(self.pool.status()["modelPool"]["nonResidentModelCount"], 0)

    async def test_past_the_limit_loads_a_non_resident_that_still_serves(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        overflow = self.pool._slots["model-c"]
        self.assertFalse(overflow.resident)
        # Non-resident means "does not consume the cap", not "is disabled":
        # it has a worker and can generate.
        self.assertIsNotNone(overflow.worker.loaded)
        self.assertIsNotNone(self.pool._pick_replica("model-c"))

    async def test_non_resident_is_reported_per_model_and_in_the_pool_summary(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        entries = {item["id"]: item for item in self.pool.status()["loadedModels"]}
        self.assertTrue(entries["model-a"]["resident"])
        self.assertFalse(entries["model-c"]["resident"])
        pool = self.pool.status()["modelPool"]
        self.assertEqual(pool["residentCount"], 3)
        self.assertEqual(pool["residentModelCount"], 2)
        self.assertEqual(pool["nonResidentModelCount"], 1)
        self.assertEqual(pool["maxResidentModels"], 2)

    async def test_non_resident_slot_is_never_session_pinned_by_the_default_pin(self):
        # `pin` defaults to true for every GUI/CLI load. It cannot mean "pin"
        # for a non-resident slot, or the owner's "unload it once the idle time
        # is up" would never happen.
        await self.pool.load(_catalog("model-a", "model-a"), pin=True)
        await self.pool.load(_catalog("model-b", "model-b"), pin=True)
        await self.pool.load(_catalog("model-c", "model-c"), pin=True)
        self.assertTrue(self.pool._slots["model-a"].session_pinned)
        self.assertFalse(self.pool._slots["model-c"].session_pinned)

    async def test_extra_replica_of_a_non_resident_model_stays_non_resident(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        await self.pool._maybe_expand_replica(self.pool._slots["model-c"], "model-c")
        for slot in self.pool._replica_slots("model-c"):
            self.assertFalse(slot.resident, f"replica {slot.replica_index}")

    async def test_pinning_a_non_resident_promotes_it_when_a_slot_is_free(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        await self.pool.unload_model("model-a")
        await self.pool.load(_catalog("model-c", "model-c"), pin=True)
        self.assertTrue(self.pool._slots["model-c"].resident)
        self.assertTrue(self.pool._slots["model-c"].session_pinned)

    async def test_pinning_a_non_resident_never_exceeds_the_cap(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        await self.pool.load(_catalog("model-c", "model-c"), pin=True)
        self.assertTrue(self.pool._slots["model-c"].session_pinned)
        self.assertFalse(self.pool._slots["model-c"].resident)
        self.assertEqual(self.pool.status()["modelPool"]["residentModelCount"], 2)

    async def test_non_resident_load_is_still_refused_when_memory_is_not_safe(self):
        # The whole point of separating "resident" from "refused" is that the
        # memory guards are untouched by the new tier.
        self.settings.data["models"]["pool"]["defaultPerModelMaxGB"] = 0.5
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(_catalog("model-a", "model-a"))
        self.assertEqual(raised.exception.code, "MODEL_MEMORY_LIMIT")

    async def test_non_resident_load_is_refused_under_os_memory_pressure(self):
        self.pool._host_capacity = lambda: (24 * GIB, 2)
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(_catalog("model-a", "model-a"))
        self.assertEqual(raised.exception.code, "MEMORY_PRESSURE")
        self.assertFalse(self.pool._slots)

    async def test_non_resident_load_is_refused_when_the_global_budget_is_full(self):
        # Three 8 GB-class models against a 10 GB budget: the third can only
        # load by evicting an idle unpinned model, and if there is none to
        # evict the load is refused rather than oversubscribing the machine.
        self.settings.data["models"]["pool"]["defaultPerModelMaxGB"] = 16
        self.pool._global_budget = lambda: 10 * GIB
        big = {"id": "big", "name": "big", "engine": "mlx-lm", "size_bytes": 6 * GIB}
        other = {"id": "other", "name": "other", "engine": "mlx-lm", "size_bytes": 6 * GIB}
        await self.pool.load(dict(big), pin=True)
        self.pool._host_capacity = lambda: (60 * GIB, 0)
        self.pool._physical_memory = lambda: 60 * GIB
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.load_for_api(dict(other))
        self.assertEqual(raised.exception.code, "MEMORY_BUDGET_EXCEEDED")

    async def test_budget_pressure_reclaims_a_non_resident_before_a_resident(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        # Fresh timestamps everywhere, so nothing is TTL-expired: only the
        # budget can be the reason anything is freed.
        for slot in self.pool._slots.values():
            slot.last_released_at = time.monotonic()
        # Room for exactly two of the three, so exactly one has to go.
        one_slot = self.pool._slots["model-a"].reservation_bytes
        self.pool._global_budget = lambda: one_slot * 2
        self.assertEqual(await self.pool._reap_once(), 1)
        self.assertNotIn("model-c", self.pool._slots)
        self.assertIn("model-a", self.pool._slots)
        self.assertIn("model-b", self.pool._slots)

    async def test_live_reduction_of_the_limit_evicts_residents_not_overflow(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        self.settings.data["models"]["pool"]["maxResidentModels"] = 1
        self.assertGreaterEqual(await self.pool._reap_once(), 1)
        survivors = {key for key in self.pool._slots}
        self.assertTrue(survivors)
        self.assertLessEqual(
            len({key for key, slot in self.pool._slots.items() if slot.resident}), 1)


class IdleExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = _store(self.root, 2)
        _reset_fake()
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", test_model_pool.FakeWorker)
        self.worker_patch.start()
        self.pool = _pool(self.root, self.settings)
        await self.pool.load_for_api(_catalog("model-a", "model-a"))

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    def _entry(self, model_id: str) -> dict:
        return {item["id"]: item for item in self.pool.status()["loadedModels"]}[model_id]

    async def test_status_reports_remaining_seconds_at_the_configured_ttl(self):
        entry = self._entry("model-a")
        self.assertEqual(entry["idleSecondsRemaining"], 30)
        self.assertIsNotNone(entry["idleExpiresAt"])
        self.assertIsNone(entry["idleExtendSeconds"])

    async def test_extension_adds_seconds_and_is_visible_in_status(self):
        result = await self.pool.extend_idle("model-a", 600)
        self.assertEqual(result["extendedSeconds"], 600)
        self.assertEqual(result["totalExtendedSeconds"], 600)
        self.assertEqual(result["idleTTLSeconds"], 30)
        entry = self._entry("model-a")
        self.assertEqual(entry["idleExtendSeconds"], 600)
        self.assertGreaterEqual(entry["idleSecondsRemaining"], 620)
        self.assertGreaterEqual(entry["idleSecondsRemaining"], 630 - 5)

    async def test_extension_keeps_the_model_past_its_original_deadline(self):
        await self.pool.extend_idle("model-a", 600)
        self.pool._slots["model-a"].last_released_at = time.monotonic() - 40
        # 40s idle, TTL 30s: without the extension it would already be gone.
        self.assertEqual(await self.pool._reap_once(), 0)
        self.assertIn("model-a", self.pool._slots)
        self.pool._slots["model-a"].last_released_at = time.monotonic() - 700
        self.assertEqual(await self.pool._reap_once(), 1)
        self.assertNotIn("model-a", self.pool._slots)

    async def test_extensions_accumulate_within_the_bound(self):
        await self.pool.extend_idle("model-a", 100)
        await self.pool.extend_idle("model-a", 50)
        self.assertEqual(self._entry("model-a")["idleExtendSeconds"], 150)
        await self.pool.extend_idle("model-a", MAX_IDLE_EXTENSION_SECONDS)
        self.assertEqual(self._entry("model-a")["idleExtendSeconds"],
                         MAX_IDLE_EXTENSION_SECONDS)

    async def test_extension_is_consumed_by_the_next_use_of_the_model(self):
        await self.pool.extend_idle("model-a", 600)
        self.assertEqual(self._entry("model-a")["idleExtendSeconds"], 600)
        async for _ in self.pool.generate_for_model(
                "model-a", "hello", [], {}, "req-1"):
            pass
        # One idle period only: the countdown restarts at the configured TTL, so
        # repeating the extension can never become a hidden permanent pin.
        self.assertIsNone(self._entry("model-a")["idleExtendSeconds"])
        self.assertLessEqual(self._entry("model-a")["idleSecondsRemaining"], 30)

    async def test_extension_is_refused_for_a_pinned_model(self):
        await self.pool.load(_catalog("model-a", "model-a"), pin=True)
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.extend_idle("model-a", 60)
        self.assertEqual(raised.exception.code, "MODEL_NOT_IDLE_MANAGED")

    async def test_extension_is_refused_for_a_model_that_is_not_loaded(self):
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.extend_idle("model-nope", 60)
        self.assertEqual(raised.exception.code, "MODEL_NOT_IDLE_MANAGED")

    async def test_extension_rejects_values_outside_the_supported_range(self):
        for bad in (0, -1, MAX_IDLE_EXTENSION_SECONDS + 1):
            with self.assertRaises(MLXBarError) as raised:
                await self.pool.extend_idle("model-a", bad)
            self.assertEqual(raised.exception.code, "INVALID_IDLE_EXTENSION")

    async def test_extension_rejects_a_non_numeric_value(self):
        with self.assertRaises(MLXBarError) as raised:
            await self.pool.extend_idle("model-a", "abc")
        self.assertEqual(raised.exception.code, "INVALID_IDLE_EXTENSION")

    async def test_extension_works_for_a_non_resident_model(self):
        await self.pool.load_for_api(_catalog("model-b", "model-b"))
        await self.pool.load_for_api(_catalog("model-c", "model-c"))
        result = await self.pool.extend_idle("model-c", 300)
        self.assertFalse(result["resident"])
        self.assertEqual(self._entry("model-c")["idleExtendSeconds"], 300)

    async def test_extension_applies_to_every_replica_of_the_model(self):
        self.settings.data["models"]["pool"]["maxReplicasPerModel"] = 2
        await self.pool._load_replica({"id": "model-a", "name": "model-a",
                                       "engine": "mlx-lm", "size_bytes": GIB},
                                      "mlx-lm", "model-a", 1, pin=False)
        self.assertEqual(len(self.pool._replica_slots("model-a")), 2)
        await self.pool.extend_idle("model-a", 120)
        for slot in self.pool._replica_slots("model-a"):
            self.assertEqual(slot.idle_extend_seconds, 120)


class NonResidentHTTPTests(unittest.IsolatedAsyncioTestCase):
    """The OpenAI-compatible surface: an API request must reach the model it
    named, whether or not that model holds a resident slot."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = _store(self.root, 1)
        _reset_fake()
        self.worker_patch = patch(
            "mlxbar.workers.model_pool.SingleWorkerSupervisor", test_model_pool.FakeWorker)
        self.worker_patch.start()
        self.pool = _pool(self.root, self.settings)
        self.database = Database(self.root / "state.sqlite3")
        self.database.replace_models([_catalog("model-a", "model-a"),
                                      _catalog("model-b", "model-b")])
        # The management `/status` route reads these AppState attributes
        # directly, so the stand-in state has to carry them.
        self.state = SimpleNamespace(settings=self.settings, workers=self.pool,
                                     database=self.database,
                                     model_autoload_lock=asyncio.Lock(),
                                     public_listener_error=None)
        import httpx
        from mlxbar.main import make_management_app
        self.client_cls = httpx.AsyncClient
        self.transport = httpx.ASGITransport(app=make_management_app(self.state))

    async def asyncTearDown(self):
        await self.pool.shutdown()
        self.worker_patch.stop()
        self.temporary.cleanup()

    async def test_api_request_loads_the_requested_model_beyond_the_limit(self):
        # The OpenAI-compatible listener is a different app from the management
        # one, so this scenario drives both over their own transports.
        import httpx
        public = httpx.ASGITransport(app=make_public_app(self.state))
        async with self.client_cls(transport=public, base_url="http://t") as client:
            first = await client.post("/v1/chat/completions", json={
                "model": "model-a", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(first.status_code, 200)
            second = await client.post("/v1/chat/completions", json={
                "model": "model-b", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json()["model"], "model-b")
        self.assertFalse(self.pool._slots["model-b"].resident)

    async def test_extend_endpoint_returns_the_new_countdown(self):
        await self.pool.load_for_api(_catalog("model-a", "model-a"))
        import httpx
        async with self.client_cls(transport=self.transport, base_url="http://t") as client:
            response = await client.post("/api/v1/models/model-a/idle/extend",
                                         json={"seconds": 900})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["id"], "model-a")
            self.assertEqual(payload["extendedSeconds"], 900)
            self.assertGreaterEqual(payload["idleSecondsRemaining"], 900)
            status = (await client.get("/api/v1/status")).json()
            entry = {item["id"]: item for item in status["loadedModels"]}["model-a"]
            self.assertGreaterEqual(entry["idleSecondsRemaining"], 900)
        del httpx

    async def test_extend_endpoint_rejects_a_bad_value_with_422(self):
        import httpx
        async with self.client_cls(transport=self.transport, base_url="http://t") as client:
            response = await client.post("/api/v1/models/model-a/idle/extend",
                                         json={"seconds": 0})
            # The management listener has no OpenAI-style error envelope, so a
            # 422 carries the code under `detail`.
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"]["code"], "INVALID_IDLE_EXTENSION")
            missing = await client.post("/api/v1/models/model-a/idle/extend", json={})
            self.assertEqual(missing.status_code, 422)
        del httpx


def _response(payload):
    client = Mock()
    client.request.return_value = Mock()
    client.request.return_value.json.return_value = payload
    return client


class ExtendIdleCLITests(unittest.TestCase):
    def test_extend_idle_posts_the_requested_seconds(self):
        client = _response({"id": "m1", "idleSecondsRemaining": 630})
        args = SimpleNamespace(command="model", action="extend-idle",
                               model_id="m1", seconds="600")
        execute(args, client)
        client.request.assert_called_once_with(
            "POST", "/api/v1/models/m1/idle/extend", {"seconds": 600})

    def test_extend_idle_rejects_a_non_integer_before_any_request(self):
        for raw in ("abc", "", "12.5", "0", "-5"):
            client = _response({})
            args = SimpleNamespace(command="model", action="extend-idle",
                                   model_id="m1", seconds=raw)
            with self.assertRaises(ValueError, msg=raw):
                execute(args, client)
            client.request.assert_not_called()

    def test_model_resident_reports_residency_and_seconds_remaining(self):
        payload = {
            "modelPool": {"residentCount": 3, "residentModelCount": 2,
                          "nonResidentModelCount": 1, "maxResidentModels": 2,
                          "idleTTLSeconds": 900},
            "loadedModels": [
                {"id": "m1", "resident": True, "idleSecondsRemaining": 850,
                 "idleExtendSeconds": None, "idleExpiresAt": 1.0},
                {"id": "m2", "resident": False, "idleSecondsRemaining": 120,
                 "idleExtendSeconds": 300, "idleExpiresAt": 2.0},
            ],
        }
        client = _response(payload)
        args = SimpleNamespace(command="model", action="resident")
        result = execute(args, client)
        self.assertEqual(result["nonResidentModelCount"], 1)
        first, second = result["models"]
        self.assertTrue(first["resident"])
        self.assertEqual(first["idleSecondsRemaining"], 850)
        self.assertFalse(second["resident"])
        self.assertEqual(second["idleSecondsRemaining"], 120)
        self.assertEqual(second["idleExtendSeconds"], 300)


if __name__ == "__main__":
    unittest.main()