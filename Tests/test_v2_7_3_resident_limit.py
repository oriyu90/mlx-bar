"""v2.7.3 resident-count limit, as superseded by v2.8.0.

v2.7.3 made the limit a refusal: below `maxResidentModels` a requested model
became a new resident, and at the limit a new distinct model failed loudly
(RESIDENT_LIMIT_REACHED, 409) instead of silently evicting.

v2.8.0 keeps the part that was right -- nothing is ever silently evicted to
make room, and same-model reloads/replicas never count as new -- and replaces
the refusal with a second tier. A model admitted once the limit is full loads
as a NON-resident: it answers requests, it does not occupy one of the
`maxResidentModels` slots, and the idle TTL frees it.

`test_below_limit_adds_the_requested_model_as_a_new_resident`,
`test_same_model_reload_is_allowed_when_full`,
`test_queue_plus_pressure_sheds_idle_transient_without_waiting_ttl` and
`test_warning_pressure_without_queue_keeps_fresh_transient` still hold
unchanged; the two refusal tests are replaced by non-resident equivalents and
the full non-resident/TTL-extension contract lives in
`test_v2_8_overflow_idle.py`.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).parents[1] / "Coordinator"))

import test_model_pool  # noqa: E402
from mlxbar.database import Database  # noqa: E402

from mlxbar.main import make_public_app  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor  # noqa: E402


def _catalog(mid: str, name: str):
    return {"id": mid, "source": "filesystem", "name": name, "path": "/tmp/" + name,
            "provider_key": None, "format": "mlx", "engine": "mlx-lm",
            "modalities": ["text"], "confidence": 1.0, "reason": "test", "size_bytes": 1}


def _store(root: Path, max_n: int) -> SettingsStore:
    store = SettingsStore(root)
    store.data["api"]["requireToken"] = False
    store.data["models"]["pool"].update({
        "enabled": True, "maxResidentModels": max_n, "minimumSystemReserveGB": 1,
        "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
    })
    return store


def test_below_limit_adds_the_requested_model_as_a_new_resident():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = _store(root, 2)
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([_catalog("model-a", "model-a"),
                                     _catalog("model-b", "model-b")])
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 1)
                state = SimpleNamespace(settings=store, workers=pool, database=database,
                                        model_autoload_lock=asyncio.Lock())
                transport = httpx.ASGITransport(app=make_public_app(state))
                try:
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        assert (await client.post("/v1/chat/completions", json={
                            "model": "model-a",
                            "messages": [{"role": "user", "content": "hi"}]})).json()["model"] == "model-a"
                        second = await client.post("/v1/chat/completions", json={
                            "model": "model-b",
                            "messages": [{"role": "user", "content": "hi"}]})
                        assert second.status_code == 200
                        assert second.json()["model"] == "model-b"
                        assert set(pool._slots) == {"model-a", "model-b"}
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_at_limit_loads_the_requested_model_as_non_resident_without_evicting():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = _store(root, 2)
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([_catalog("model-a", "model-a"),
                                     _catalog("model-b", "model-b"),
                                     _catalog("model-c", "model-c")])
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 1)
                state = SimpleNamespace(settings=store, workers=pool, database=database,
                                        model_autoload_lock=asyncio.Lock())
                transport = httpx.ASGITransport(app=make_public_app(state))
                try:
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        for name in ("model-a", "model-b"):
                            assert (await client.post("/v1/chat/completions", json={
                                "model": name,
                                "messages": [{"role": "user", "content": "hi"}]})).status_code == 200
                        third = await client.post("/v1/chat/completions", json={
                            "model": "model-c",
                            "messages": [{"role": "user", "content": "hi"}]})
                        assert third.status_code == 200
                        assert third.json()["model"] == "model-c"
                        # Nothing was evicted to make room, and the two
                        # residents kept their slots.
                        assert set(pool._slots) == {"model-a", "model-b", "model-c"}
                        assert pool._slots["model-a"].resident
                        assert pool._slots["model-b"].resident
                        assert not pool._slots["model-c"].resident
                        # Freeing a resident promotes nothing by itself; the
                        # next *new* model takes the freed slot.
                        await pool.unload_model("model-a")
                        assert set(pool._slots) == {"model-b", "model-c"}
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_same_model_reload_is_allowed_when_full():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = _store(root, 1)
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([_catalog("model-a", "model-a")])
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 1)
                try:
                    await pool.load_for_api(_catalog("model-a", "model-a"))
                    again = await pool.load_for_api(_catalog("model-a", "model-a"))
                    assert again["id"] == "model-a"
                    assert set(pool._slots) == {"model-a"}
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_explicit_pin_load_at_the_limit_loads_transient_instead_of_failing():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = _store(root, 1)
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 1)
                try:
                    await pool.load(_catalog("model-a", "model-a"), pin=True)
                    await pool.load(_catalog("model-b", "model-b"), pin=True)
                    assert set(pool._slots) == {"model-a", "model-b"}
                    # `pin` is the load API's default, so a GUI/CLI load past
                    # the limit has to mean something: the slot loads, but
                    # non-resident means NOT pinned, so it is the idle TTL that
                    # frees it and the resident slot stays with model-a.
                    assert pool._slots["model-a"].session_pinned
                    assert not pool._slots["model-b"].session_pinned
                    assert not pool._slots["model-b"].resident
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_queue_plus_pressure_sheds_idle_transient_without_waiting_ttl():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 3, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
                "idleTTLSeconds": 900,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 0)
                try:
                    await pool.load_for_api({"id": "idle-a", "name": "idle-a",
                                             "engine": "mlx-lm", "size_bytes": GIB})
                    await pool.load_for_api({"id": "idle-b", "name": "idle-b",
                                             "engine": "mlx-lm", "size_bytes": GIB})
                    # Fresh (far from TTL) but a queue backlog exists elsewhere.
                    pool._host_capacity = lambda: (24 * GIB, 2)
                    busy = pool._slots["idle-a"]
                    busy.gen_queued["waiter-1"] = object()
                    try:
                        # Leased/queued holders are protected; the other idle
                        # transient is shed despite its fresh timestamp.
                        busy.leases = 1
                        assert await pool._reap_once() == 1
                        assert set(pool._slots) == {"idle-a"}
                    finally:
                        busy.gen_queued.clear()
                        busy.leases = 0
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_warning_pressure_without_queue_keeps_fresh_transient():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 3, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
                "idleTTLSeconds": 900,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = ModelPoolSupervisor(root, store)
                pool._physical_memory = lambda: 32 * GIB
                pool._host_capacity = lambda: (24 * GIB, 0)
                try:
                    await pool.load_for_api({"id": "fresh", "name": "fresh",
                                             "engine": "mlx-lm", "size_bytes": GIB})
                    pool._host_capacity = lambda: (24 * GIB, 2)
                    assert await pool._reap_once() == 0
                    assert set(pool._slots) == {"fresh"}
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())
