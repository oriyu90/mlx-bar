"""``RagService`` -- the single entry point the rest of MLXBar uses for RAG.

Holds the vector store and builds an :class:`EmbeddingClient` on demand from
the current settings. Nothing here runs unless a caller asks: the store file is
created on the first ``create_collection``, and the request-time hook
(:meth:`retrieve_context_block`) is only reached when a request carries a
``rag`` field *and* ``rag.enabled`` is set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..errors import MLXBarError
from .chunking import split_text
from .embeddings import EmbeddingClient
from .retrieval import build_context_block, rank
from .store import RagStore, normalize_name

# A hard cap on a single ingested document, independent of settings: a
# multi-megabyte paste should be rejected early, not chunked into thousands of
# embedding calls.
MAX_DOCUMENT_CHARS = 2_000_000


class RagService:
    def __init__(self, root: Path, settings):
        self.root = Path(root)
        self.settings = settings
        self.store = RagStore(self.root / "rag.sqlite3")

    def close(self) -> None:
        self.store.close()

    # -- config -------------------------------------------------------

    def _config(self) -> dict:
        return (self.settings.data or {}).get("rag", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self._config().get("enabled", False))

    def require_enabled(self) -> None:
        if not self.enabled:
            raise MLXBarError("RAG_DISABLED",
                              "ナレッジベース機能が無効です。設定 > 知識ベース で有効にしてください",
                              400, False)

    def _client(self) -> EmbeddingClient:
        config = self._config()
        embedding = config.get("embedding", {}) or {}
        token = None
        getter = getattr(self.settings, "rag_embedding_token", None)
        if isinstance(getter, str):
            token = getter or None
        return EmbeddingClient(
            embedding.get("baseUrl", ""),
            embedding.get("model", ""),
            token=token,
            timeout_seconds=embedding.get("timeoutSeconds", 30),
            batch_size=embedding.get("batchSize", 32),
        )

    # -- collections / documents -----------------------------------

    def list_collections(self) -> dict:
        return {"data": self.store.list_collections()}

    def create_collection(self, name: str) -> dict:
        return self.store.create_collection(name)

    def delete_collection(self, name: str) -> dict:
        return self.store.delete_collection(name)

    def list_documents(self, collection: str) -> dict:
        return {"collection": normalize_name(collection),
                "data": self.store.list_documents(collection)}

    def delete_document(self, collection: str, document_id: str) -> dict:
        return self.store.delete_document(collection, document_id)

    # -- ingest ----------------------------------------------------

    def _read_path(self, path: str) -> tuple[str, str]:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise MLXBarError("RAG_FILE_NOT_FOUND",
                              f"ファイルが見つかりません: {path}", 400, False)
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise MLXBarError("RAG_FILE_NOT_FOUND", str(exc), 400, False) from exc
        if size > MAX_DOCUMENT_CHARS * 4:
            raise MLXBarError("RAG_DOCUMENT_TOO_LARGE",
                              f"ファイルが大きすぎます（上限 約{MAX_DOCUMENT_CHARS // 1000}k文字）",
                              413, False)
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise MLXBarError("RAG_FILE_NOT_FOUND", str(exc), 400, False) from exc
        return text, candidate.name

    async def ingest(self, collection: str, *, text: str | None = None,
                     path: str | None = None, title: str | None = None,
                     progress=None) -> dict:
        """Chunk -> embed -> store one document. Raises ``MLXBarError`` on failure."""
        self.require_enabled()
        name = normalize_name(collection)
        self.store.require_collection(name)
        source = ""
        if path:
            text, source = self._read_path(path)
            title = title or source
        body = (text or "").strip()
        if not body:
            raise MLXBarError("RAG_EMPTY_DOCUMENT", "取り込む本文が空です", 400, False)
        if len(body) > MAX_DOCUMENT_CHARS:
            raise MLXBarError("RAG_DOCUMENT_TOO_LARGE",
                              f"本文が長すぎます（上限 {MAX_DOCUMENT_CHARS:,} 文字）", 413, False)
        config = self._config()
        pieces = split_text(body, config.get("chunkSize", 1000), config.get("chunkOverlap", 200))
        if not pieces:
            raise MLXBarError("RAG_EMPTY_DOCUMENT", "分割できるテキストがありません", 400, False)
        if progress:
            await progress(0.3, f"{len(pieces)}個のチャンクを埋め込み中")
        client = self._client()
        vectors = await client.embed(pieces)
        if len(vectors) != len(pieces):
            raise MLXBarError("RAG_EMBEDDING_UNAVAILABLE",
                              "埋め込みの件数がチャンク数と一致しません", 502, True)
        if progress:
            await progress(0.85, "保存中")
        return await asyncio.to_thread(
            self.store.add_document, name,
            title=(title or "untitled"), source=source,
            chunks=list(zip(pieces, vectors)),
            embedding_model=client.model,
            max_chunks_per_collection=config.get("maxChunksPerCollection", 5000),
        )

    def ingest_job(self, jobs, collection: str, *, text: str | None = None,
                   path: str | None = None, title: str | None = None) -> dict:
        async def work(update):
            await update(0.1, "取り込みを開始しています")
            return await self.ingest(collection, text=text, path=path, title=title,
                                     progress=update)
        return jobs.create(f"rag_ingest:{normalize_name(collection)}", work)

    # -- query / retrieval --------------------------------------

    async def query(self, collection: str, query_text: str, top_k: int | None = None) -> dict:
        self.require_enabled()
        name = normalize_name(collection)
        self.store.require_collection(name)
        query_text = (query_text or "").strip()
        if not query_text:
            raise MLXBarError("RAG_EMPTY_QUERY", "クエリが空です", 400, False)
        config = self._config()
        k = int(top_k) if top_k else int(config.get("defaultTopK", 4))
        chunks = await asyncio.to_thread(self.store.load_chunks, name)
        if not chunks:
            return {"collection": name, "topK": k, "results": []}
        query_vector = await self._client().embed_one(query_text)
        results = rank(query_vector, chunks, k)
        return {"collection": name, "topK": k,
                "results": [{"text": item["text"], "score": item["score"],
                             "documentId": item["documentId"],
                             "documentTitle": item["documentTitle"],
                             "ordinal": item["ordinal"]} for item in results]}

    async def retrieve_context_block(self, spec: dict, query_text: str,
                                     language: str = "en") -> tuple[str, dict]:
        """Request-time hook: return ``(context_block, summary)``.

        ``spec`` is the request's ``rag`` object. Raises ``MLXBarError`` for an
        unknown collection or (unless ``spec['optional']``) an unreachable
        embedding backend.
        """
        self.require_enabled()
        collection = normalize_name(spec.get("collection", ""))
        self.store.require_collection(collection)
        config = self._config()
        top_k = spec.get("topK") or spec.get("top_k") or config.get("defaultTopK", 4)
        try:
            top_k = max(1, min(int(top_k), 20))
        except (TypeError, ValueError):
            top_k = int(config.get("defaultTopK", 4))
        max_chars = spec.get("maxChars") or spec.get("max_chars") or config.get("maxContextChars", 6000)
        try:
            max_chars = max(200, min(int(max_chars), 32000))
        except (TypeError, ValueError):
            max_chars = int(config.get("maxContextChars", 6000))

        query_text = (query_text or "").strip()
        if not query_text:
            return "", {"collection": collection, "passages": 0, "skipped": "empty_query"}

        chunks = await asyncio.to_thread(self.store.load_chunks, collection)
        if not chunks:
            return "", {"collection": collection, "passages": 0, "skipped": "empty_collection"}
        try:
            query_vector = await self._client().embed_one(query_text)
        except MLXBarError:
            if spec.get("optional"):
                return "", {"collection": collection, "passages": 0,
                            "skipped": "embedding_unavailable"}
            raise
        ranked = rank(query_vector, chunks, top_k)
        block = build_context_block(ranked, max_chars, language)
        summary = {"collection": collection, "passages": len(ranked),
                   "topScore": ranked[0]["score"] if ranked else None,
                   "chars": len(block)}
        return block, summary

    # -- status --------------------------------------------------

    async def status(self, *, probe: bool = True) -> dict:
        config = self._config()
        embedding = config.get("embedding", {}) or {}
        collections = self.store.list_collections()
        result = {
            "enabled": self.enabled,
            "embedding": {"baseUrl": embedding.get("baseUrl", ""),
                          "model": embedding.get("model", ""),
                          "timeoutSeconds": embedding.get("timeoutSeconds", 30),
                          "batchSize": embedding.get("batchSize", 32),
                          "tokenConfigured": bool(getattr(self.settings, "rag_embedding_token", None))},
            "chunkSize": config.get("chunkSize", 1000),
            "chunkOverlap": config.get("chunkOverlap", 200),
            "defaultTopK": config.get("defaultTopK", 4),
            "maxContextChars": config.get("maxContextChars", 6000),
            "maxChunksPerCollection": config.get("maxChunksPerCollection", 5000),
            "collections": collections,
            "collectionCount": len(collections),
            "chunkCount": sum(int(item.get("chunk_count", 0)) for item in collections),
        }
        if probe:
            try:
                result["backend"] = await self._client().probe()
            except MLXBarError as exc:
                result["backend"] = {"reachable": False, "code": exc.code, "message": exc.message}
        return result
