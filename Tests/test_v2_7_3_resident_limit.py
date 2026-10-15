"""v2.7.3 strict resident-count limit: add while room, error when full.

Owner request: while the resident count is below maxResidentModels, an API
request for another catalog-known model is added (loaded) as a new resident.
Once distinct residents reach the limit, a new distinct model is refused
loudly (RESIDENT_LIMIT_REACHED, 409) instead of silently evicting.
Same-model reloads/replicas never count as new and stay allowed.
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
from mlxbar.errors import MLXBarError  # noqa: E402
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


def test_at_limit_returns_resident_limit_reached_without_evicting():
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
                        assert third.status_code == 409
                        assert third.json()["error"]["code"] == "RESIDENT_LIMIT_REACHED"
                        assert set(pool._slots) == {"model-a", "model-b"}
                        # Explicit unload frees one slot; the next request loads.
                        await pool.unload_model("model-a")
                        retry = await client.post("/v1/chat/completions", json={
                            "model": "model-c",
                            "messages": [{"role": "user", "content": "hi"}]})
                        assert retry.status_code == 200
                        assert retry.json()["model"] == "model-c"
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


def test_explicit_pin_load_is_also_strict_when_full():
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
                    try:
                        await pool.load(_catalog("model-b", "model-b"), pin=True)
                    except MLXBarError as exc:
                        assert exc.code == "RESIDENT_LIMIT_REACHED"
                    else:
                        raise AssertionError("expected RESIDENT_LIMIT_REACHED")
                    assert set(pool._slots) == {"model-a"}
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())
