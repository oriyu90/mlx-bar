"""v2.7.2 model-routing regression: strict request honoring + stable residency.

Covers the two owner-reported issues on v2.7.1:

1. A request for a real, catalog-known distinct model while another model is
   resident must honor the request (autoload/switch), never silently answer
   with the resident -- including maxResidentModels == 1.
2. A resident must never be recognized as a different model: exact
   id/name/provider_key wins over generic aliases, generic aliases return the
   stable primary without flipping it, and provider_key None never matches
   "none".
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
from mlxbar.api.openai_compat import (  # noqa: E402
    _candidate_values,
    _find_exact_model,
    _find_model,
    _honor_resident_alias,
    _model_matches,
)
from mlxbar.database import Database  # noqa: E402
from mlxbar.main import make_public_app  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402
from mlxbar.workers.model_pool import GIB, ModelPoolSupervisor  # noqa: E402


def _catalog(mid: str, name: str):
    return {"id": mid, "source": "filesystem", "name": name, "path": "/tmp/" + name,
            "provider_key": None, "format": "mlx", "engine": "mlx-lm",
            "modalities": ["text"], "confidence": 1.0, "reason": "test", "size_bytes": 1}


async def _pool_with(root: Path, store: SettingsStore, database: Database):
    pool = ModelPoolSupervisor(root, store)
    pool._physical_memory = lambda: 32 * GIB
    pool._host_capacity = lambda: (24 * GIB, 1)
    return pool


def test_max1_switches_to_a_known_distinct_model():
    # v2.7.3: strict resident-count limit supersedes the v2.7.2 switch-by-
    # eviction. With maxResidentModels == 1 and one distinct resident, a
    # request for another distinct catalog model is refused loudly
    # (RESIDENT_LIMIT_REACHED) instead of silently evicting. The caller
    # unloads the current resident explicitly, then the request loads.
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["api"]["requireToken"] = False
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 1, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([_catalog("model-a", "model-a"), _catalog("model-b", "model-b")])
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = await _pool_with(root, store, database)
                state = SimpleNamespace(settings=store, workers=pool, database=database,
                                        model_autoload_lock=asyncio.Lock())
                transport = httpx.ASGITransport(app=make_public_app(state))
                try:
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        first = await client.post("/v1/chat/completions", json={
                            "model": "model-a", "messages": [{"role": "user", "content": "hi"}]})
                        assert first.status_code == 200
                        assert first.json()["model"] == "model-a"
                        second = await client.post("/v1/chat/completions", json={
                            "model": "model-b", "messages": [{"role": "user", "content": "hi"}]})
                        assert second.status_code == 409
                        assert second.json()["error"]["code"] == "RESIDENT_LIMIT_REACHED"
                        assert set(pool._slots) == {"model-a"}
                        await pool.unload_model("model-a")
                        third = await client.post("/v1/chat/completions", json={
                            "model": "model-b", "messages": [{"role": "user", "content": "hi"}]})
                        assert third.status_code == 200
                        assert third.json()["model"] == "model-b"
                        assert set(pool._slots) == {"model-b"}
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_unknown_name_is_404_not_silent_resident():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["api"]["requireToken"] = False
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 1, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([_catalog("model-a", "model-a")])
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = await _pool_with(root, store, database)
                state = SimpleNamespace(settings=store, workers=pool, database=database,
                                        model_autoload_lock=asyncio.Lock())
                transport = httpx.ASGITransport(app=make_public_app(state))
                try:
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        assert (await client.post("/v1/chat/completions", json={
                            "model": "model-a",
                            "messages": [{"role": "user", "content": "hi"}]})).status_code == 200
                        unknown = await client.post("/v1/chat/completions", json={
                            "model": "definitely-no-such-model",
                            "messages": [{"role": "user", "content": "hi"}]})
                        assert unknown.status_code == 404
                        assert unknown.json()["error"]["code"] == "MODEL_NOT_FOUND"
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_generic_alias_still_routes_to_the_resident():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["api"]["requireToken"] = False
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 1, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([_catalog("model-a", "model-a")])
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = await _pool_with(root, store, database)
                state = SimpleNamespace(settings=store, workers=pool, database=database,
                                        model_autoload_lock=asyncio.Lock())
                transport = httpx.ASGITransport(app=make_public_app(state))
                try:
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        assert (await client.post("/v1/chat/completions", json={
                            "model": "model-a",
                            "messages": [{"role": "user", "content": "hi"}]})).status_code == 200
                        for alias in ("loaded", "local", "x", "openai/x", "current_model"):
                            response = await client.post("/v1/chat/completions", json={
                                "model": alias,
                                "messages": [{"role": "user", "content": "hi"}]})
                            assert response.status_code == 200, alias
                            assert response.json()["model"] == "model-a", alias
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_exact_name_wins_over_generic_alias():
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.sqlite3")
        database.replace_models([
            _catalog("x", "x"),
            {**_catalog("other", "Other"), "id": "other"},
        ])
        database.set_metadata_value("last_loaded_model_id", "other")
        state = SimpleNamespace(database=database)
        assert _find_model(state, "x")["id"] == "x"


def test_find_returns_exact_without_flipping_primary_to_first():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 2, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = await _pool_with(root, store, Database(root / "state.sqlite3"))
                try:
                    await pool.load({"id": "model-a", "name": "model-a",
                                     "engine": "mlx-lm", "size_bytes": GIB}, pin=True)
                    await pool.load({"id": "x", "name": "x",
                                     "engine": "mlx-lm", "size_bytes": GIB}, pin=True)
                    assert pool._primary_model_id == "x"
                    found = pool.find_loaded_model("x")
                    assert found and found.get("id") == "x"
                    assert pool._primary_model_id == "x"
                    aliased = pool.find_loaded_model("loaded")
                    assert aliased and aliased.get("id") == "x"
                    assert pool._primary_model_id == "x"
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_none_provider_key_never_matches_none():
    model = {"id": "a", "name": "ModelA", "provider_key": None}
    assert "none" not in _candidate_values(model)
    assert not _model_matches(model, "none", None)
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "state.sqlite3")
        database.replace_models([_catalog("a", "ModelA")])
        state = SimpleNamespace(database=database)
        assert _find_model(state, "none") is None


def test_per_model_default_max_tokens_prefers_requested_over_primary():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 2, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = await _pool_with(root, store, Database(root / "state.sqlite3"))
                try:
                    await pool.load({"id": "model-a", "name": "model-a",
                                     "engine": "mlx-lm", "size_bytes": GIB}, pin=True)
                    await pool.load({"id": "model-b", "name": "model-b",
                                     "engine": "mlx-lm", "size_bytes": GIB}, pin=True)
                    pool._slots["model-a"].worker.effective_max_tokens = lambda: 111
                    pool._slots["model-b"].worker.effective_max_tokens = lambda: 222
                    pool._primary_model_id = "model-a"
                    assert pool.effective_max_tokens_for("model-b") == 222
                    assert pool.effective_max_tokens() == 111
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())


def test_alias_shaped_real_model_is_autoloaded_not_aliased():
    # A real catalog model whose id looks like a generic alias ("x") must
    # autoload when it is not resident, not answer with the current resident.
    # Pure generic aliases with no exact catalog match still alias.
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SettingsStore(root)
            store.data["api"]["requireToken"] = False
            store.data["models"]["pool"].update({
                "enabled": True, "maxResidentModels": 3, "minimumSystemReserveGB": 1,
                "defaultPerModelMaxGB": 8, "generationConcurrency": 2,
            })
            test_model_pool.FakeWorker.instances = []
            test_model_pool.FakeWorker.load_gate = None
            database = Database(root / "state.sqlite3")
            database.replace_models([_catalog("model-a", "model-a"), _catalog("x", "x")])
            with patch("mlxbar.workers.model_pool.SingleWorkerSupervisor",
                        test_model_pool.FakeWorker):
                pool = await _pool_with(root, store, database)
                try:
                    await pool.load({"id": "model-a", "name": "model-a",
                                     "engine": "mlx-lm", "size_bytes": GIB}, pin=True)
                    state = SimpleNamespace(settings=store, workers=pool, database=database,
                                            model_autoload_lock=asyncio.Lock())
                    assert _find_exact_model(state, "x")["id"] == "x"
                    assert not _honor_resident_alias(
                        state, {"id": "model-a", "name": "model-a"}, "x")
                    assert _honor_resident_alias(
                        state, {"id": "model-a", "name": "model-a"}, "loaded")
                    assert not _honor_resident_alias(
                        state, {"id": "model-a", "name": "model-a"}, "no-such-model")
                    transport = httpx.ASGITransport(app=make_public_app(state))
                    async with httpx.AsyncClient(transport=transport,
                                                 base_url="http://t") as client:
                        response = await client.post("/v1/chat/completions", json={
                            "model": "x",
                            "messages": [{"role": "user", "content": "hi"}]})
                        assert response.status_code == 200
                        assert response.json()["model"] == "x"
                        assert set(pool._slots) == {"model-a", "x"}
                finally:
                    await pool.shutdown()
    asyncio.run(scenario())
