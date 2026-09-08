# MLXBar v2.1.0 テスト計画と結果

対象: ローカル知識ベース（RAG）の追加。文書分割 → 外部 OpenAI 互換 `/v1/embeddings` での
埋め込み生成 → 純 Python のベクトル検索 → 取得文のプロンプト注入。設計は `DESIGN_v2.1.0.md`。

## 1. 自動検証（Python）

```sh
cd Coordinator && uv run pytest ../Tests -q -p no:randomly
```

2026-09-08 結果:

- **430 passed**（v2.0.1 の 410 + `test_rag.py` 新規16 + `test_cli.py` 新規4）
- ランダム順（`-p no:randomly` なし）でも 430 passed
- 既知フレーク `test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats` は
  今回の連続実行では再現せず（§0-N・過去記録どおり据え置き）

## 2. 本リリースの新規契約

`Tests/test_rag.py`（16件）:

| 区分 | 検証 |
|---|---|
| チャンク分割 | サイズ・オーバーラップの遵守、空/短文、区切りなしテキストのハード折返し |
| ストア | 遅延生成（読み取りではファイルを作らない）、コレクション/ドキュメント CRUD、カスケード削除、不正名・次元不一致・チャンク上限の拒否 |
| 検索 | コサイン順位、`rank()` が embedding を落とす、context block の文字上限と「最低1件」 |
| サービス | ingest → query の往復とスコア降順、`rag.enabled=false` での `RAG_DISABLED` |
| retrieve フック | `optional:true` のフォールバック、既定の 503、コレクション不明の 404 |
| リクエスト統合（OpenAI） | `rag` 未指定時はメッセージ無改変、`rag` 指定時に先頭 system へ注入、`RAG_DISABLED`(400)/`RAG_COLLECTION_NOT_FOUND`(404)、`response_format: json_object` との共存 |
| 管理 API | コレクション作成→（ジョブ）→ドキュメント一覧→query→status→削除の一巡、不正名の 400 |
| 設定バリデーション | `rag` の各範囲（chunkSize/chunkOverlap/defaultTopK/baseUrl/maxChunksPerCollection）|

`Tests/test_cli.py`（4件、`RagCliTests`）:

| # | 契約 | 検証 |
|---|---|---|
| 1 | `config set-rag` は指定した項目だけ patch する（`embedding` はネストでマージ） | `test_set_rag_only_sends_provided_options` |
| 2 | `config set-rag` は範囲外・空指定を拒否する | `test_set_rag_rejects_out_of_range_and_empty` |
| 3 | `rag collection create` / `rag query` が期待エンドポイントを叩く | `test_rag_collection_and_query_hit_expected_endpoints` |
| 4 | `rag doc add` は `--file` と `--text` の排他を強制する | `test_rag_doc_add_requires_exactly_one_source` |

埋め込みバックエンドはテスト内の `FakeEmbeddingClient`（語彙 bag-of-words の決定的ベクトル）で
差し替え。`RagService._client` を monkeypatch する。

## 3. 回帰

- v2.0.1 の 410 件が無改変で緑。`rag` フィールドを付けない `/v1/chat/completions` と
  `/anthropic/v1/messages` の既存テストはすべて緑（`_apply_rag` は `body.get("rag") is None`
  で即戻る）。
- 設定スキーマは追加のみ。`schemaVersion` は 1 のまま。`test_core.py` の設定関連が緑。
- 追加した `/api/v1/status` の `rag` キー、`loadedModels[]` は無変更、`modelPool` は無変更。

## 4. Swift ビルド

```sh
swift build --disable-sandbox
```

2026-09-08: 成功（Swift 6 strict concurrency）。変更は `MenuBarViewModel`（RAG 用
`@Published` とメソッド、`/api/v1/status` の `rag` パース、struct 3種）、
`MLXBarSettingsView`（タブ追加）、`MenuBarView`（1行表示）、`FileSelectionService`
（`chooseTextFile` 追加）、新規 `KnowledgeBaseSettingsView.swift`、
`en.lproj/Localizable.strings` に2キー追加。

## 5. 実機確認（Apple Silicon）

`/Applications/MLXBar.app` を v2.1.0 へ入れ替え、coordinator 再起動後に確認する:

| ケース | 期待 |
|---|---|
| `/api/v1/health` | `{"status":"ok","version":"2.1.0"}` |
| `rag.enabled` 既定（false）で通常の `/v1/chat/completions` | v2.0.1 と同一挙動、回帰なし。`rag.sqlite3` は未生成 |
| LM Studio 等を埋め込みバックエンドに設定 → `mlxbarctl rag collection create kb` → `rag doc add kb --file …` | チャンク数が返り、`rag doc list kb` に1件 |
| `rag` 付き `/v1/chat/completions`（`{"collection":"kb","topK":4}`） | 先頭に取得文の system メッセージが入り、`/api/v1/status` の `rag.lastRetrieval` と メニューバーに表示 |
| 埋め込みバックエンド停止中に `rag` 付きリクエスト | HTTP 503 `RAG_EMBEDDING_UNAVAILABLE`（`retryable: true`）|
| 同上で `"rag": {"optional": true}` | 文脈なしで通常生成にフォールバック（200）|
| `rag.enabled=false` のまま `rag` 付きリクエスト | HTTP 400 `RAG_DISABLED` |
| GUI「設定 > 知識ベース」タブ | 日本語／英語のどちらでも崩れず、接続テスト・コレクション作成・テキスト追加・検索テストが動作 |

APIトークン・リクエスト本文・個人パスは成果物に含めない。埋め込みトークンは
`control/rag-embedding-token`（0600）にのみ保存。

## 6. ビルド

```sh
./scripts/build-release.sh
VERSION=2.1.0 ./scripts/verify-release.sh
shasum -a 256 dist/MLXBar-2.1.0.dmg > dist/MLXBar-2.1.0.dmg.sha256
```

DMG SHA-256: `eefe3bee2ae176dcd83011b95a566b6d169e25520335da4e4f91a3e4999be938`
