# MLXBar v2.1.0

A local knowledge base (RAG): document splitting, embedding generation, vector
search and context assembly, added entirely inside the coordinator with no new
dependency and no new worker type. **Off by default** -- a request that does not
carry a `rag` field behaves exactly as in v2.0.1.

コーディネータ内に依存追加なしで実装したローカル知識ベース（RAG）。文書分割・埋め込み生成・
ベクトル検索・コンテキスト生成。**既定で無効**で、`rag` フィールドを付けないリクエストの
挙動は v2.0.1 と完全に同一です。

## Added: local knowledge base (`Coordinator/mlxbar/rag/`)

- **Chunking** (`chunking.py`): recursive character splitting (blank line -> line
  -> sentence punctuation -> space -> hard wrap), configurable `chunkSize` /
  `chunkOverlap` in characters. Pure Python.
- **Vector store** (`store.py`): its own SQLite file, `rag.sqlite3`, opened
  lazily -- it is not created until the first collection. Same single-connection
  + `RLock` + transaction discipline as `database.Database`. Collections cascade
  to their documents and chunks on delete.
- **Embeddings** (`embeddings.py`): a client for an **external** OpenAI-compatible
  `/v1/embeddings` endpoint (LM Studio, Ollama, llama.cpp, a cloud provider).
  MLXBar does not compute embeddings itself. Batched, timeout-bounded, one retry;
  every failure becomes a typed `MLXBarError`
  (`RAG_EMBEDDING_UNAVAILABLE` / `RAG_EMBEDDING_DIM_MISMATCH` /
  `RAG_EMBEDDING_NOT_CONFIGURED`). `asyncio.CancelledError` is never swallowed.
- **Retrieval** (`retrieval.py`): pure-Python cosine top-k; the retrieved
  passages are assembled into one synthetic `system` message, capped at
  `maxContextChars`, with a bilingual header.
- **`RagService`** (`service.py`): ties the above together; ingestion runs as a
  `jobs.py` job. A single document is capped at 2,000,000 characters.

## Added: optional `rag` field on chat requests

`/v1/chat/completions` and `/anthropic/v1/messages` accept an optional `rag`
object, e.g. `{"collection": "my-notes", "topK": 4, "maxChars": 6000,
"optional": false}`. The most recent user message is used as the query; the
retrieved context is prepended as its own `system` message, composing with
`response_format` injection and `contextCompression` (retrieval runs after
compression). Absent -> byte-identical to v2.0.1.

- Unknown collection -> HTTP 404 `RAG_COLLECTION_NOT_FOUND`.
- `rag` present while `rag.enabled` (setting) is off -> HTTP 400 `RAG_DISABLED`.
- Embedding endpoint unreachable -> HTTP 503 `RAG_EMBEDDING_UNAVAILABLE`
  (`retryable: true`). Send `"rag": {"optional": true}` to fall back to a normal
  generation with no context instead.

## Added: management API, CLI, GUI

- `GET /api/v1/rag/status`, `GET|POST /api/v1/rag/collections`,
  `DELETE /api/v1/rag/collections/{name}`,
  `GET|POST /api/v1/rag/collections/{name}/documents`,
  `DELETE .../documents/{id}`, `POST /api/v1/rag/collections/{name}/query`,
  `GET|PUT /api/v1/settings/rag-embedding-token`. `/api/v1/status` gains a `rag`
  summary; `reset_all()` cleans up `rag.sqlite3`.
- `mlxbarctl rag status | collection … | doc … | query`, and the GUI-equivalent
  `mlxbarctl config set-rag --…` (only the options you pass change), plus
  `mlxbarctl secrets get-rag-embedding-token | set-rag-embedding-token`.
- A new **Settings > Knowledge base** tab (Japanese and English): master toggle,
  embedding backend + connection test, chunking, collection CRUD, document add
  (file / text) / list / delete, and a query test. The menu bar shows the most
  recent retrieval.

## Compatibility / 互換性

- `rag.enabled = false` (default) -> the feature is fully dormant; an ordinary
  request never reaches the `rag` package and `rag.sqlite3` is not created.
- A separate SQLite file -> `state.sqlite3` is untouched, no migration.
- Settings schema is additive; `schemaVersion` stays 1. An existing `config.json`
  needs no migration. The new `/api/v1/status` `rag` key is additive; an older
  GUI ignores it.
- Worker / Coordinator<->Worker RPC / prompt cache / model pool / the OpenAI and
  Anthropic wire formats (with no `rag` field): unchanged.

## Verification / 検証

- Python regression suite: **430 passed** (410 from v2.0.1 + 16 new in
  `test_rag.py` + 4 new in `test_cli.py`). Stable in default and fixed order.
- `swift build --disable-sandbox`: succeeds.
- Design and invariants: `DESIGN_v2.1.0.md`. Test plan and on-hardware checks:
  `TEST_PLAN_v2.1.0.md`.

## Checksum

`eefe3bee2ae176dcd83011b95a566b6d169e25520335da4e4f91a3e4999be938`  `MLXBar-2.1.0.dmg`
