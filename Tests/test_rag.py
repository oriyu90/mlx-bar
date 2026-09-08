from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "Workers"))

from mlxbar.database import Database  # noqa: E402
from mlxbar.errors import MLXBarError  # noqa: E402
from mlxbar.jobs import JobManager  # noqa: E402
from mlxbar.main import make_management_app, make_public_app  # noqa: E402
from mlxbar.rag.chunking import split_text  # noqa: E402
from mlxbar.rag.retrieval import build_context_block, cosine, rank  # noqa: E402
from mlxbar.rag.service import RagService  # noqa: E402
from mlxbar.rag.store import RagStore, normalize_name  # noqa: E402
from mlxbar.settings import SettingsStore  # noqa: E402


# --- fakes ---------------------------------------------------------------

_VOCAB = ["cat", "dog", "fish", "bird", "sql", "python", "coffee", "tea", "mountain", "river"]


def _fake_vector(text: str) -> list[float]:
    lowered = text.lower()
    vector = [float(lowered.count(word)) for word in _VOCAB]
    vector.append(1.0)  # bias term so an all-zero text still has a norm
    return vector


class FakeEmbeddingClient:
    def __init__(self, *, fail=False, model="fake-embed"):
        self.fail = fail
        self.model = model
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE", "backend down", 503, True)
        return [_fake_vector(text) for text in texts]

    async def embed_one(self, text):
        return (await self.embed([text]))[0]

    async def probe(self):
        if self.fail:
            return {"reachable": False, "code": "RAG_EMBEDDING_UNAVAILABLE", "message": "down"}
        return {"reachable": True, "dim": len(_VOCAB) + 1}


class EchoWorker:
    loaded = {"id": "local-model", "name": "Local Model", "engine": "mlx-lm"}

    def __init__(self, text="ANSWER"):
        self.text = text
        self.last_messages = None

    def find_loaded_model(self, requested):
        return self.loaded

    def loaded_models(self):
        return [self.loaded]

    def find_resident_model(self, requested):
        return self.loaded

    def raise_if_queue_full(self):
        return None

    def effective_max_tokens(self):
        return 4096

    async def generate_for_model(self, model_id, messages, images, options, request_id, image_root=None):
        self.last_messages = messages
        yield {"type": "delta", "text": self.text}
        yield {"type": "completed", "finish_reason": "stop"}
        yield {"type": "usage", "prompt_tokens": 10, "completion_tokens": 2}

    async def generate(self, messages, images, options, request_id, image_root=None):
        async for event in self.generate_for_model(None, messages, images, options, request_id):
            yield event


def _make_state(tmp: Path, *, enabled=True, fail_embed=False, worker=None):
    settings = SettingsStore(tmp)
    settings.data["api"]["requireToken"] = False
    settings.data["rag"]["enabled"] = enabled
    database = Database(tmp / "state.sqlite3")
    database.replace_models([{
        "id": "local-model", "name": "Local Model", "engine": "mlx-lm",
        "format": "mlx", "path": "/models/local", "modalities": ["text"],
        "source": "filesystem", "provider_key": None, "confidence": 1.0,
        "size_bytes": 1 << 30, "reason": "",
    }])
    rag = RagService(tmp, settings)
    fake = FakeEmbeddingClient(fail=fail_embed)
    rag._client = lambda: fake  # type: ignore[assignment]
    state = SimpleNamespace(
        settings=settings, database=database, rag=rag,
        jobs=JobManager(database), workers=worker or EchoWorker(),
        last_context_compression=None, last_rag_retrieval=None,
        model_autoload_lock=asyncio.Lock(),
        root=tmp, public_listener_error=None,
    )
    return state, fake


# --- chunking ----------------------------------------------------------

def test_split_text_respects_size_and_overlap():
    text = "。".join(f"文{i}" * 40 for i in range(20))
    chunks = split_text(text, chunk_size=200, chunk_overlap=50)
    assert chunks
    assert all(len(chunk) <= 260 for chunk in chunks)  # size + a little slack
    # consecutive chunks share some text (overlap)
    assert any(chunks[i][-10:] in chunks[i + 1] for i in range(len(chunks) - 1))


def test_split_text_blank_and_short():
    assert split_text("   ") == []
    assert split_text("short") == ["short"]


def test_split_text_hard_wraps_text_without_separators():
    chunks = split_text("x" * 5000, chunk_size=1000, chunk_overlap=0)
    assert len(chunks) >= 5
    assert all(len(chunk) <= 1000 for chunk in chunks)


# --- store ------------------------------------------------------------

def test_store_is_lazy_then_crud_and_cascade():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "rag.sqlite3"
        store = RagStore(path)
        assert store.list_collections() == []
        assert not path.exists()  # nothing created by a read

        store.create_collection("notes")
        assert path.exists()
        with pytest.raises(MLXBarError):
            store.create_collection("notes")  # duplicate

        doc = store.add_document(
            "notes", title="t", source="s",
            chunks=[("a cat", [1.0, 0.0]), ("a dog", [0.0, 1.0])],
            embedding_model="fake", max_chunks_per_collection=100)
        assert doc["chunkCount"] == 2
        assert store.get_collection("notes")["dim"] == 2
        assert len(store.load_chunks("notes")) == 2

        store.delete_document("notes", doc["id"])
        assert store.load_chunks("notes") == []

        store.add_document("notes", title="t2", source="", chunks=[("x", [1.0, 1.0])],
                           embedding_model="fake", max_chunks_per_collection=100)
        summary = store.delete_collection("notes")
        assert summary["deleted"] == "notes"
        assert store.list_collections() == []


def test_store_rejects_bad_names_and_dim_mismatch_and_full():
    with tempfile.TemporaryDirectory() as directory:
        store = RagStore(Path(directory) / "rag.sqlite3")
        for bad in ("", "has space", "-leading", "x" * 65, "slash/name"):
            with pytest.raises(MLXBarError):
                normalize_name(bad)
        store.create_collection("c")
        store.add_document("c", title="t", source="", chunks=[("a", [1.0, 0.0])],
                           embedding_model="fake", max_chunks_per_collection=100)
        with pytest.raises(MLXBarError) as excinfo:
            store.add_document("c", title="t", source="", chunks=[("b", [1.0, 0.0, 0.0])],
                               embedding_model="fake", max_chunks_per_collection=100)
        assert excinfo.value.code == "RAG_EMBEDDING_DIM_MISMATCH"
        with pytest.raises(MLXBarError) as excinfo:
            store.add_document("c", title="t", source="",
                               chunks=[("b", [0.0, 1.0]), ("d", [1.0, 1.0])],
                               embedding_model="fake", max_chunks_per_collection=1)
        assert excinfo.value.code == "RAG_COLLECTION_FULL"


# --- retrieval ------------------------------------------------------

def test_cosine_and_rank_order():
    chunks = [
        {"id": 1, "text": "the cat sat", "documentId": "d", "documentTitle": "T", "ordinal": 0,
         "embedding": _fake_vector("the cat sat")},
        {"id": 2, "text": "python and sql", "documentId": "d", "documentTitle": "T", "ordinal": 1,
         "embedding": _fake_vector("python and sql")},
        {"id": 3, "text": "a dog barks", "documentId": "d", "documentTitle": "T", "ordinal": 2,
         "embedding": _fake_vector("a dog barks")},
    ]
    ranked = rank(_fake_vector("tell me about the cat"), chunks, top_k=2)
    assert len(ranked) == 2
    assert ranked[0]["text"] == "the cat sat"
    assert "embedding" not in ranked[0]
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_build_context_block_caps_chars_but_keeps_one():
    chunks = [{"text": "A" * 500, "documentTitle": "Doc", "documentId": "d", "ordinal": 0},
              {"text": "B" * 500, "documentTitle": "Doc", "documentId": "d", "ordinal": 1}]
    block = build_context_block(chunks, max_chars=400, language="ja")
    assert "A" in block and "B" not in block  # only the first fits
    assert len(block) <= 420


# --- service ------------------------------------------------------

def test_service_ingest_query_and_disabled_guard():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            state, _ = _make_state(tmp, enabled=True)
            rag = state.rag
            rag.create_collection("kb")
            result = await rag.ingest("kb", text="Cats purr. Dogs bark. Fish swim in the river.",
                                      title="animals")
            assert result["chunkCount"] >= 1
            query = await rag.query("kb", "tell me about cats", top_k=2)
            assert query["results"] and query["results"][0]["score"] >= query["results"][-1]["score"]

            state.settings.data["rag"]["enabled"] = False
            with pytest.raises(MLXBarError) as excinfo:
                await rag.ingest("kb", text="x", title="y")
            assert excinfo.value.code == "RAG_DISABLED"

    asyncio.run(scenario())


def test_service_retrieve_context_block_optional_fallback_and_hard_error():
    async def scenario():
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            state, fake = _make_state(tmp, enabled=True)
            state.rag.create_collection("kb")
            await state.rag.ingest("kb", text="The mountain river is cold.", title="geo")

            fake.fail = True
            block, summary = await state.rag.retrieve_context_block(
                {"collection": "kb", "optional": True}, "river")
            assert block == "" and summary["skipped"] == "embedding_unavailable"

            with pytest.raises(MLXBarError) as excinfo:
                await state.rag.retrieve_context_block({"collection": "kb"}, "river")
            assert excinfo.value.code == "RAG_EMBEDDING_UNAVAILABLE"

            with pytest.raises(MLXBarError) as excinfo:
                await state.rag.retrieve_context_block({"collection": "missing"}, "river")
            assert excinfo.value.code == "RAG_COLLECTION_NOT_FOUND"

    asyncio.run(scenario())


# --- request integration (OpenAI) -------------------------------

def test_chat_without_rag_field_is_untouched():
    with tempfile.TemporaryDirectory() as directory:
        state, _ = _make_state(Path(directory))
        worker = state.workers
        client = TestClient(make_public_app(state))
        response = client.post("/v1/chat/completions", json={
            "model": "local-model", "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200
        assert all(message.get("role") != "system" for message in worker.last_messages)


def test_chat_with_rag_field_injects_context_system_message():
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        state, _ = _make_state(tmp, enabled=True)
        asyncio.run(_seed(state, "kb", "Espresso is strong coffee. Green tea has caffeine too."))
        worker = state.workers
        client = TestClient(make_public_app(state))
        response = client.post("/v1/chat/completions", json={
            "model": "local-model",
            "messages": [{"role": "user", "content": "tell me about coffee"}],
            "rag": {"collection": "kb", "topK": 2}})
        assert response.status_code == 200
        assert worker.last_messages[0]["role"] == "system"
        assert "coffee" in worker.last_messages[0]["content"].lower()
        assert state.last_rag_retrieval["passages"] >= 1


def test_chat_rag_disabled_returns_400_and_unknown_collection_404():
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        state, _ = _make_state(tmp, enabled=False)
        client = TestClient(make_public_app(state))
        disabled = client.post("/v1/chat/completions", json={
            "model": "local-model", "messages": [{"role": "user", "content": "hi"}],
            "rag": {"collection": "kb"}})
        assert disabled.status_code == 400
        assert disabled.json()["error"]["code"] == "RAG_DISABLED"

        state.settings.data["rag"]["enabled"] = True
        missing = client.post("/v1/chat/completions", json={
            "model": "local-model", "messages": [{"role": "user", "content": "hi"}],
            "rag": {"collection": "nope"}})
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RAG_COLLECTION_NOT_FOUND"


def test_chat_rag_coexists_with_response_format_json_object():
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        state, _ = _make_state(tmp, enabled=True, worker=EchoWorker('{"ok": true}'))
        asyncio.run(_seed(state, "kb", "The river flows past the mountain."))
        worker = state.workers
        client = TestClient(make_public_app(state))
        response = client.post("/v1/chat/completions", json={
            "model": "local-model",
            "messages": [{"role": "user", "content": "describe the river"}],
            "response_format": {"type": "json_object"},
            "rag": {"collection": "kb"}})
        assert response.status_code == 200
        systems = [m for m in worker.last_messages if m["role"] == "system"]
        joined = " ".join(m["content"] for m in systems)
        assert "river" in joined.lower() and "JSON" in joined


# --- management API ------------------------------------------------

def test_management_rag_endpoints_roundtrip():
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        state, _ = _make_state(tmp, enabled=True)
        client = TestClient(make_management_app(state))

        assert client.get("/api/v1/rag/collections").json() == {"data": []}
        created = client.post("/api/v1/rag/collections", json={"name": "kb"})
        assert created.status_code == 201

        # The add-document endpoint hands back an ingest job.
        job = client.post("/api/v1/rag/collections/kb/documents",
                          json={"text": "Coffee keeps you awake.", "title": "c"})
        assert job.status_code == 202 and job.json()["kind"].startswith("rag_ingest:")

        # Rebuild the collection and seed deterministically for the list/query
        # assertions below (JobManager tasks are timing-dependent under the
        # TestClient event loop, so the job above may or may not have landed).
        state.rag.store.delete_collection("kb")
        state.rag.create_collection("kb")
        asyncio.run(state.rag.ingest("kb", text="Coffee keeps you awake.", title="c"))

        docs = client.get("/api/v1/rag/collections/kb/documents").json()
        assert len(docs["data"]) == 1

        query = client.post("/api/v1/rag/collections/kb/query", json={"query": "coffee"})
        assert query.status_code == 200 and query.json()["results"]

        status = client.get("/api/v1/rag/status?probe=false").json()
        assert status["enabled"] is True and status["collectionCount"] == 1

        assert client.delete("/api/v1/rag/collections/kb").status_code == 200
        assert client.get("/api/v1/rag/collections").json() == {"data": []}


def test_management_rag_invalid_name_rejected():
    with tempfile.TemporaryDirectory() as directory:
        state, _ = _make_state(Path(directory), enabled=True)
        client = TestClient(make_management_app(state))
        bad = client.post("/api/v1/rag/collections", json={"name": "has space"})
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "RAG_INVALID_NAME"


# --- settings validation ------------------------------------------

def test_settings_validation_rag_ranges():
    with tempfile.TemporaryDirectory() as directory:
        settings = SettingsStore(Path(directory))
        settings.update({"rag": {"chunkSize": 500, "chunkOverlap": 100, "defaultTopK": 6}})
        assert settings.data["rag"]["chunkSize"] == 500
        for bad in ({"rag": {"chunkSize": 50}},
                    {"rag": {"chunkOverlap": 9999}},
                    {"rag": {"defaultTopK": 0}},
                    {"rag": {"embedding": {"baseUrl": "ftp://x"}}},
                    {"rag": {"maxChunksPerCollection": 10}}):
            with pytest.raises(ValueError):
                settings.update(bad)


async def _seed(state, collection: str, text: str) -> None:
    state.rag.create_collection(collection)
    await state.rag.ingest(collection, text=text, title="seed")
