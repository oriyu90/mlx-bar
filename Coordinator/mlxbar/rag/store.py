"""SQLite-backed vector store for the knowledge base.

Its own file (``rag.sqlite3``), separate from the app database, so the RAG
feature can never migrate, lock or corrupt ``state.sqlite3`` -- and so a
knowledge base can be wiped on its own. The connection is opened lazily: until
the first collection is created the file does not exist, and a read against a
missing store returns empty rather than creating it.

Concurrency model mirrors ``database.Database``: one connection guarded by an
``RLock``, every write inside a ``with connection`` transaction.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path

from ..errors import MLXBarError

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS collections (
  name TEXT PRIMARY KEY,
  embedding_model TEXT,
  dim INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  collection TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  char_count INTEGER NOT NULL DEFAULT 0,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  collection TEXT NOT NULL,
  document_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL DEFAULT 0,
  text TEXT NOT NULL,
  embedding TEXT NOT NULL,
  dim INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chunks_collection ON chunks(collection);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
"""


def normalize_name(value: object) -> str:
    """Validate a collection name and return it unchanged.

    Names travel into URL paths and SQL, so the accepted set is deliberately
    narrow: a leading alphanumeric then up to 63 more of ``[A-Za-z0-9._-]``.
    """
    if not isinstance(value, str) or not _NAME_RE.match(value.strip()):
        raise MLXBarError(
            "RAG_INVALID_NAME",
            "コレクション名は英数字で始まり、英数字・ドット・ハイフン・アンダースコアのみ、64文字以内で指定してください",
            400, False,
        )
    return value.strip()


class RagStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    # -- connection -------------------------------------------------------

    def _conn(self, *, create: bool) -> sqlite3.Connection | None:
        with self.lock:
            if self._connection is not None:
                return self._connection
            if not create and not self.path.exists():
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.executescript(SCHEMA)
            self._connection = connection
            return connection

    def close(self) -> None:
        with self.lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    # -- collections ----------------------------------------------------

    def list_collections(self) -> list[dict]:
        connection = self._conn(create=False)
        if connection is None:
            return []
        with self.lock:
            rows = connection.execute(
                """SELECT c.name, c.embedding_model, c.dim, c.created_at,
                          (SELECT COUNT(*) FROM documents d WHERE d.collection = c.name) AS document_count,
                          (SELECT COUNT(*) FROM chunks k WHERE k.collection = c.name) AS chunk_count
                   FROM collections c ORDER BY c.name COLLATE NOCASE"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_collection(self, name: str) -> dict | None:
        name = normalize_name(name)
        connection = self._conn(create=False)
        if connection is None:
            return None
        with self.lock:
            row = connection.execute(
                """SELECT c.name, c.embedding_model, c.dim, c.created_at,
                          (SELECT COUNT(*) FROM documents d WHERE d.collection = c.name) AS document_count,
                          (SELECT COUNT(*) FROM chunks k WHERE k.collection = c.name) AS chunk_count
                   FROM collections c WHERE c.name = ?""", (name,)
            ).fetchone()
        return dict(row) if row else None

    def require_collection(self, name: str) -> dict:
        collection = self.get_collection(name)
        if collection is None:
            raise MLXBarError("RAG_COLLECTION_NOT_FOUND",
                              f"コレクション「{name}」が見つかりません", 404, False)
        return collection

    def create_collection(self, name: str) -> dict:
        name = normalize_name(name)
        connection = self._conn(create=True)
        assert connection is not None
        with self.lock, connection:
            existing = connection.execute(
                "SELECT 1 FROM collections WHERE name = ?", (name,)).fetchone()
            if existing:
                raise MLXBarError("RAG_COLLECTION_EXISTS",
                                  f"コレクション「{name}」は既に存在します", 409, False)
            connection.execute("INSERT INTO collections(name) VALUES(?)", (name,))
        return self.get_collection(name) or {"name": name, "dim": None,
                                             "embedding_model": None,
                                             "document_count": 0, "chunk_count": 0}

    def delete_collection(self, name: str) -> dict:
        name = normalize_name(name)
        collection = self.require_collection(name)
        connection = self._conn(create=False)
        assert connection is not None
        with self.lock, connection:
            connection.execute("DELETE FROM chunks WHERE collection = ?", (name,))
            connection.execute("DELETE FROM documents WHERE collection = ?", (name,))
            connection.execute("DELETE FROM collections WHERE name = ?", (name,))
        return {"deleted": name, "documents": collection.get("document_count", 0),
                "chunks": collection.get("chunk_count", 0)}

    # -- documents ----------------------------------------------------

    def add_document(self, collection: str, *, title: str, source: str,
                     chunks: list[tuple[str, list[float]]], embedding_model: str,
                     max_chunks_per_collection: int) -> dict:
        """Insert one document and its chunks in a single transaction.

        Enforces the per-collection chunk ceiling and that every document in a
        collection shares one embedding dimension.
        """
        name = normalize_name(collection)
        current = self.require_collection(name)
        if not chunks:
            raise MLXBarError("RAG_EMPTY_DOCUMENT", "取り込む本文がありません", 400, False)
        dim = len(chunks[0][1])
        if dim <= 0 or any(len(vector) != dim for _, vector in chunks):
            raise MLXBarError("RAG_EMBEDDING_DIM_MISMATCH",
                              "埋め込みベクトルの次元が揃っていません", 502, True)
        existing_dim = current.get("dim")
        if existing_dim and int(existing_dim) != dim:
            raise MLXBarError(
                "RAG_EMBEDDING_DIM_MISMATCH",
                f"コレクション「{name}」は次元{existing_dim}で作成されています（今回の埋め込みは{dim}次元）。"
                "埋め込みモデルを変更した場合はコレクションを作り直してください",
                409, False,
            )
        remaining = int(max_chunks_per_collection) - int(current.get("chunk_count", 0))
        if len(chunks) > remaining:
            raise MLXBarError(
                "RAG_COLLECTION_FULL",
                f"コレクション「{name}」のチャンク上限（{max_chunks_per_collection}）に達しました。"
                "不要なドキュメントを削除するか、上限を引き上げてください",
                409, False,
            )
        document_id = f"doc-{uuid.uuid4().hex[:16]}"
        char_count = sum(len(text) for text, _ in chunks)
        connection = self._conn(create=True)
        assert connection is not None
        with self.lock, connection:
            connection.execute(
                "INSERT INTO documents(id,collection,title,source,char_count,chunk_count) "
                "VALUES(?,?,?,?,?,?)",
                (document_id, name, title or "", source or "", char_count, len(chunks)),
            )
            connection.executemany(
                "INSERT INTO chunks(collection,document_id,ordinal,text,embedding,dim) "
                "VALUES(?,?,?,?,?,?)",
                [(name, document_id, ordinal, text, json.dumps(vector), dim)
                 for ordinal, (text, vector) in enumerate(chunks)],
            )
            if not existing_dim:
                connection.execute(
                    "UPDATE collections SET dim = ?, embedding_model = ? WHERE name = ?",
                    (dim, embedding_model, name),
                )
        return {"id": document_id, "collection": name, "title": title or "",
                "source": source or "", "charCount": char_count, "chunkCount": len(chunks)}

    def list_documents(self, collection: str) -> list[dict]:
        name = normalize_name(collection)
        self.require_collection(name)
        connection = self._conn(create=False)
        if connection is None:
            return []
        with self.lock:
            rows = connection.execute(
                "SELECT id,title,source,char_count,chunk_count,created_at "
                "FROM documents WHERE collection = ? ORDER BY created_at DESC, id DESC",
                (name,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_document(self, collection: str, document_id: str) -> dict:
        name = normalize_name(collection)
        self.require_collection(name)
        connection = self._conn(create=False)
        assert connection is not None
        with self.lock, connection:
            row = connection.execute(
                "SELECT chunk_count FROM documents WHERE collection = ? AND id = ?",
                (name, document_id),
            ).fetchone()
            if row is None:
                raise MLXBarError("RAG_DOCUMENT_NOT_FOUND",
                                  "指定されたドキュメントが見つかりません", 404, False)
            connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        return {"deleted": document_id, "chunks": int(row["chunk_count"])}

    # -- chunks ----------------------------------------------------

    def load_chunks(self, collection: str) -> list[dict]:
        """Every chunk of a collection with its embedding decoded to a list.

        This is the whole working set for a query. It is bounded by
        ``rag.maxChunksPerCollection`` (enforced on write), so the caller can
        rely on it fitting in memory.
        """
        name = normalize_name(collection)
        connection = self._conn(create=False)
        if connection is None:
            return []
        with self.lock:
            rows = connection.execute(
                "SELECT k.id, k.document_id, k.ordinal, k.text, k.embedding, "
                "       d.title AS document_title "
                "FROM chunks k LEFT JOIN documents d ON d.id = k.document_id "
                "WHERE k.collection = ? ORDER BY k.id", (name,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            try:
                vector = json.loads(row["embedding"])
            except (TypeError, ValueError):
                continue
            if isinstance(vector, list) and vector:
                result.append({"id": row["id"], "documentId": row["document_id"],
                               "ordinal": row["ordinal"], "text": row["text"],
                               "documentTitle": row["document_title"] or "",
                               "embedding": vector})
        return result
