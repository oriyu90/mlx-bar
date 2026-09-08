# MLXBar v2.1.0 設計書（ローカル知識ベース / RAG）

更新日: 2026-09-08
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:** LM Studio を参考にした RAG パイプライン（文書分割 → 埋め込み生成 →
ベクトル検索 → コンテキスト生成）を MLXBar に追加する。ローカルのドキュメント集合を
「コレクション」として管理し、`/v1/chat/completions` または `/anthropic/v1/messages` の
リクエストに `rag` フィールドを付けたときだけ、関連する文章を検索してプロンプト先頭へ
注入する。GUI（設定 > 知識ベース）・CLI（`mlxbarctl rag …`）・管理 API から等価に操作できる。

**非目的:**

- **MLXBar 自身で埋め込みを計算しない。** コーディネータは軽量依存（fastapi / httpx /
  pydantic / uvicorn）のまま。`mlx_embeddings` のような重い依存も、新しい Worker 種別も
  追加しない。埋め込みは外部の OpenAI 互換 `/v1/embeddings`（LM Studio、Ollama、
  llama.cpp、クラウド）へ委譲する。ネイティブ MLX 埋め込み Worker は、`logprobs` や
  OpenAI Responses API と同様に「コスト設計を先に要する将来課題」として据え置く
  （バンドルサイズ・クラッシュ安全性・メモリ挙動の実測が未了）。
- **既存の生成経路の挙動を変えない。** `rag` フィールドを付けないリクエストは v2.0.1 と
  バイト等価。設定スキーマは追加のみで `schemaVersion` は 1 のまま。既存 `config.json` は
  そのまま読める。Worker / Coordinator↔Worker RPC / プロンプトキャッシュ / モデルプール /
  OpenAI・Anthropic の wire format は無変更。
- 文法制約デコード、リランカー（cross-encoder）、ハイブリッド検索（BM25 併用）、
  マルチベクトル、PDF/HTML パーサは対象外。プレーンテキストの取り込みのみ。

## 2. アーキテクチャ

新パッケージ `Coordinator/mlxbar/rag/`。`AppState.rag`（`RagService`）としてのみ他から触る。

| モジュール | 役割 |
|---|---|
| `chunking.py` | 再帰的文字分割（段落 → 文 → 句読点 → 空白 → ハード折返し）。`chunkSize` / `chunkOverlap` は**文字数**。コーディネータにトークナイザは無いので文字予算を安定した代理とする。純 Python・依存なし。 |
| `store.py` | 別ファイル SQLite `~/Library/Application Support/MLXBar/rag.sqlite3`。`database.Database` と同じ「単一コネクション ＋ `RLock` ＋ `with connection` トランザクション」。テーブル `collections` / `documents` / `chunks`（`embedding` は JSON float 配列 ＋ `dim`）。**接続は遅延**：最初のコレクション作成まで `rag.sqlite3` を作らず、読み取りは存在しないストアに対して空を返す。コレクション削除で子をカスケード。コレクション名は `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` に制限（URL パス・SQL に載るため）。 |
| `embeddings.py` | 外部 OpenAI 互換 `/v1/embeddings` クライアント（`httpx`）。バッチ（`batchSize`）、`timeoutSeconds` 上限、コールドサーバ対策の1回リトライ。**全失敗を型付き `MLXBarError` に変換**（`RAG_EMBEDDING_UNAVAILABLE` 503 retryable / `RAG_EMBEDDING_DIM_MISMATCH` 502 / `RAG_EMBEDDING_NOT_CONFIGURED` 400）。`asyncio.CancelledError` は握らない。`probe()` は設定 UI 用の到達性チェックで例外を投げない。 |
| `retrieval.py` | 純 Python のコサイン類似度 top-k（`math` のみ）。`build_context_block(chunks, max_chars, language)` が取得チャンクを1個の合成 `system` メッセージへ整形（日英テンプレート、`[n] タイトル` 形式、`maxContextChars` で打切り、最低1件は必ず入れる）。`inject_context_block(messages, block)` は `response_format.inject_into_messages` と同じく index 0 に置く。 |
| `service.py` | `RagService`。`AppState.rag`。ingest（chunk → embed → store、`jobs.py` のジョブ）/ query / コレクション・ドキュメント CRUD / `retrieve_context_block`（リクエスト時フック）/ `status`。1ドキュメントの上限 `MAX_DOCUMENT_CHARS = 2,000,000`。 |

## 3. リクエスト統合（オプトイン・互換影響ゼロ）

`openai_compat.chat()` の `maybe_compress_messages` 呼び出し直後（`if compression:` の後、
`if body.get("stream")` の前）に `_apply_rag(request, body, normalized_messages)` を1か所追加。
stream / 非 stream / `n>1` の全経路がこの後 `normalized_messages` を共有するので1か所で足りる。
**圧縮の後に retrieval を実行**（注入した文脈ブロックを要約で消さないため）。

`anthropic_compat._messages()` の同じ位置に鏡写しで追加。取得ブロックは内部 `messages` の
先頭（Anthropic の `system` に対応）へ前置。エラー封筒は Anthropic 形式。

`rag` フィールド（未指定 = 完全に従来動作）:

```json
"rag": {"collection": "my-notes", "topK": 4, "maxChars": 6000, "optional": false}
```

- クエリ = 直近の user メッセージの本文（`_last_user_text`）。user メッセージが無ければ
  retrieval をスキップ（エラーにしない）。
- `topK` は 1..20、`maxChars` は 200..32000 にクランプ。未指定なら設定の
  `defaultTopK` / `maxContextChars`。
- `rag.enabled`（設定）が false のとき `rag` があれば HTTP 400 `RAG_DISABLED`。
- `rag.collection` が不明なら HTTP 404 `RAG_COLLECTION_NOT_FOUND`。
- 埋め込みエンドポイント到達不可 → `optional` が false（既定）なら HTTP 503
  `RAG_EMBEDDING_UNAVAILABLE`（retryable）。true なら注入せず通常生成へフォールバック
  （`response_format` の「守れない保証は返さない」方針の裏返し。`optional` は明示的な
  opt-in）。
- `response_format` 注入・`contextCompression` と両立（別々の system メッセージ）。

可視化: `AppState.last_rag_retrieval`（インメモリのみ、`last_context_compression` と同じ扱い）
→ `/api/v1/status` の `rag` フィールド → メニューバーに1行。

## 4. 管理 API（`management.py` の既存 router）

- `GET  /api/v1/rag/status`（`?probe=false` で埋め込みバックエンドへの接続を省略）
- `GET|POST /api/v1/rag/collections`、`DELETE /api/v1/rag/collections/{name}`
- `GET|POST /api/v1/rag/collections/{name}/documents`（POST は取り込みジョブを返す。
  `{text,title}` または `{path}`）、`DELETE .../documents/{id}`
- `POST /api/v1/rag/collections/{name}/query`（生成なしの取得プレビュー）
- `GET|PUT /api/v1/settings/rag-embedding-token`（`control/rag-embedding-token`、
  `lm-studio-token` と同じ `_write_secret` パターン）

`/api/v1/status` に `rag` サマリ（`enabled` / `collectionCount` / `chunkCount` /
`lastRetrieval`、ネットワークアクセスなし）。`state.reset_all()` に `rag.close()` を追加
（`rag.sqlite3` 本体は既存の「control 以外を全消去」ループが消す）。

## 5. 設定スキーマ（追加のみ）

`DEFAULTS["rag"]`：`enabled: false`、`embedding: {baseUrl, model, timeoutSeconds: 30,
batchSize: 32}`、`chunkSize: 1000`、`chunkOverlap: 200`、`defaultTopK: 4`、
`maxContextChars: 6000`、`maxChunksPerCollection: 5000`。`_validate` に範囲チェックを追加
（`contextCompression` と同じ書き方）。`chunkOverlap` は `0..chunkSize//2`。他スキーマは無変更。

## 6. CLI（`cli.py`、v1.8.1 パリティ契約）

- `rag status` / `rag collection list|create <name>|delete <name>`
- `rag doc add <collection> (--file PATH | --text STR) [--title T] [--wait]` /
  `rag doc list <collection>` / `rag doc remove <collection> <id>`
- `rag query <collection> <query> [--top-k N]`
- `config set-rag [--enabled …] [--embedding-base-url …] [--embedding-model …]
  [--embedding-timeout-seconds …] [--embedding-batch-size …] [--chunk-size …]
  [--chunk-overlap …] [--default-top-k …] [--max-context-chars …]
  [--max-chunks-per-collection …]`（`set-model-pool` と同じ「指定した項目だけ変更」）
- `secrets get-rag-embedding-token` / `set-rag-embedding-token [token]`

## 7. GUI（`KnowledgeBaseSettingsView.swift`、新規タブ「知識ベース」）

`MLXBarSettingsView` の `pages` に `"知識ベース"` を追加。セクション: マスタートグル /
埋め込みバックエンド（base URL・モデル・トークン・接続テスト）/ チャンク設定 /
コレクション CRUD / 選択コレクションのドキュメント（ファイル追加＝既存
`FileSelectionService.chooseTextFile`／テキスト追加／一覧／削除）/ 検索テスト。
`MenuBarViewModel` に `@Published`（`ragEnabled` / `ragCollections` / `ragDocuments` /
`ragQueryResults` / `ragBackendReachable` / `ragEmbeddingDim` / `ragEmbeddingToken` /
`ragStatusMessage` / `ragLastRetrievalText`）とメソッド群、`/api/v1/status` の `rag` を
防御的にパース。メニューバーに `ragLastRetrievalText` の1行。

**日英完全対応:** この新規ビューはランタイム補間を含む文字列が多いため、`LS()` テーブルでは
なく `model.guiLanguage` に対する二値ヘルパー `t(_ ja:_ en:)` で全文字列を解決する
（`ui()` パターンの拡張）。`en.lproj/Localizable.strings` へはタブ名とファイルピッカーの
2キーのみ追加。

## 8. 安全性・不変条件

- **`rag.enabled = false`（既定）で機能は完全に休眠。** 通常リクエストは `_apply_rag` の
  1行目（`body.get("rag") is None`）で戻る。`rag.sqlite3` はコレクション作成まで作られない。
- **別 SQLite ファイル** のため既存 `state.sqlite3` は無変更・マイグレーションなし。
- **クラッシュ安全性:** 埋め込み I/O はすべてタイムアウト境界付き＋型付き例外。
  `asyncio.CancelledError` は伝播。ストアは `database.py` と同じロック規約。
  不正なコレクション名は 400、次元不一致は明示エラー。
- **メモリ安全性:** `maxChunksPerCollection`（既定 5000）でコレクションを上限管理。
  取得時は1コレクション分のベクトルのみをメモリに読む（`load_chunks`）。5000 × dim 個の
  Python float を一時的に保持する（dim 1024 なら数十〜100 MB オーダーの一過性、通常は
  はるかに小さい）。ingest はジョブ化＋バッチ埋め込みでイベントループを塞がない。
  `--file` 読み取りはサイズ上限付き、テキストはそのまま保存、コード実行なし。
- **互換性:** 追加した `modelPool` 相当の新フィールド（`/api/v1/status` の `rag`）は増分。
  旧 GUI は無視。`rag` 未指定の OpenAI/Anthropic リクエストは v2.0.1 とバイト等価。

## 9. 検証

`TEST_PLAN_v2.1.0.md` を参照。Python 回帰 **430件**（v2.0.1 の 410 + `test_rag.py` 16 +
`test_cli.py` 4）。固定順・ランダム順とも緑。`swift build --disable-sandbox` 成功。
実機で「無効時の回帰なし」「LM Studio 等を埋め込みバックエンドにしてコレクション作成 →
ドキュメント追加 → `rag` 付き chat でコンテキスト注入」「バックエンド停止時に 503」
「`rag.optional:true` でフォールバック」「GUI タブが日英で崩れない」を確認する。
