# MLXBar v2.2.0 テスト計画と結果

対象: 実験的機能「アダプティブ・ハイブリッド・コンテキストメモリ」の追加。設計は
`DESIGN_v2.2.0.md`。既定で無効（`experimental.adaptiveMemory.enabled = false`）。

## 1. 自動検証（Python）

```sh
cd Coordinator && uv run pytest ../Tests -q -p no:randomly
```

2026-09-11 結果:

- **466 passed**（v2.1.0 の 430 + `test_adaptive_memory.py` 新規34 + `test_cli.py` 新規2）
- ランダム順（`-p no:randomly` なし）でも 466 passed

## 2. 本リリースの新規契約

`Tests/test_adaptive_memory.py`（34件、MLX は import しない・fake のみ）:

| 区分 | 検証 |
|---|---|
| 設定 | 既定 OFF・`softToken` 既定 OFF、各範囲（policy / triggerRatio / memoryPressureRatio / keepTailMessages / maxLatentTokens / maxRetrievedSegments / bool 群）の拒否、正当な patch の受理 |
| 排他 | `adaptiveMemory.enabled` と `contextCompression.enabled` を両方 true にすると `ValueError`（順序どちらでも）|
| Context Structure Analyzer | コード / diff / パス / ハッシュ / 数値密度 / shell / tool traffic / 画像 → EXACT、素の散文 → LATENT 候補 |
| Policy プリセット | `fidelity` は `memorySaver` より LATENT が多く COLD が少ない |
| ノートキャッシュ | 往復＋再オープンで残る、truncate されたファイルは miss |
| Planner（OpenAI 経路 E2E） | 既定 OFF はメッセージ無改変・サマライザ呼び出しなし・`adaptiveMemory` オプション非送出。ON で中央を COLD ドロップ＋LATENT ノート、tail と system は逐語、`compression_ratio < 1`、`last_adaptive_memory` 更新 |
| フォールバック | ノート生成失敗 → その run は逐語保持、`last_adaptive_memory` は None、応答は返る |
| 内容アドレス | 同一履歴の再送で 2 回目はサマライザを呼ばない（全部キャッシュ）|
| Worker オプション伝播 | `softToken` ON のとき real 呼び出しの `options.adaptiveMemory.softToken == true`、`policyVersion == "am1"` |
| Worker policy | 短いプロンプトはバンド分割不可（None）、長いプロンプトは head+band+tail == total、band ≥ 512、tail ≥ 256 |
| Worker cache key | `hybrid_key` は 7 つの fingerprint のどれを変えても変化、`latent_key` は内容アドレス |
| Worker fail-closed | Reader が `input_embeddings` 非対応 → `FallbackToExact`、埋め込み層なし → `FallbackToExact`、短いプロンプト → `FallbackToExact`、`accepts_input_embeddings` の probe が正しい |
| Planner の頑健性 | Worker が同期例外を投げても `maybe_plan_adaptive_memory` は投げず `(messages, None)`、malformed 入力（None / str / [] / 1 メッセージ）で no-op |

`Tests/test_cli.py`（2件、`AdaptiveMemoryCliTests` 相当）:

| # | 契約 | 検証 |
|---|---|---|
| 1 | `config set-adaptive-memory` は指定した項目だけ patch する（`experimental.adaptiveMemory` へネスト）| `test_set_adaptive_memory_only_sends_provided_options` |
| 2 | `config set-adaptive-memory` は範囲外・空指定を拒否する | `test_set_adaptive_memory_rejects_out_of_range_and_empty` |

## 3. 回帰

- v2.1.0 の 430 件が無改変で緑。`adaptiveMemory` を含まない `/v1/chat/completions` と
  `/anthropic/v1/messages` の既存テストはすべて緑（`maybe_plan_adaptive_memory` は
  `enabled` チェックで即戻る）。
- 設定スキーマは追加のみ。`schemaVersion` は 1 のまま。`test_core.py` の設定関連が緑。
- `context_compression` の既存テスト（`test_context_compression.py`）は無改変で緑
  （`_summarize` / `_split_point` / `_effective_max_prompt_characters` を re-export しただけ）。
- 追加した `/api/v1/status` の `adaptiveMemory` キーは増分。`loadedModels[]` / `modelPool` /
  `contextCompression` / `rag` は無変更。
- `Workers/mlx_lm_worker/adapter.py` の OFF 経路: `adaptive_cfg = {}` で二重ゲートが False、
  `adaptive_active` は常に False、`PromptCacheStore.fetch` / `_remember` は従来どおり。
  `test_prompt_cache_reuse.py` / `test_worker_server.py` が緑。

## 4. Swift ビルド

```sh
swift build --disable-sandbox
```

2026-09-11: 成功（Swift 6 strict concurrency）。変更は `MLXBarSettingsView`
（「実験的機能」セクション追加、`.task` で `experimental.adaptiveMemory` を読み込み）、
`MenuBarViewModel`（`setAdaptiveMemorySettings`、`@Published` 2種、`/api/v1/status`
パース、`adaptiveMemorySummaryText`）、`MenuBarView`（1行表示）、
`en.lproj/Localizable.strings` に v2.2.0 ブロックを追加。`plutil -lint` OK。
使用中の全 JP キーに EN 訳あり。

## 5. 実機確認（Apple Silicon）

`/Applications/MLXBar.app` を v2.2.0 へ入れ替え、coordinator 再起動後に確認する:

| ケース | 期待 |
|---|---|
| `/api/v1/health` | `{"status":"ok","version":"2.2.0"}` |
| `experimental.adaptiveMemory.enabled` 既定（false）で通常の `/v1/chat/completions` | v2.1.0 と同一挙動、回帰なし。`adaptive-memory/` は未生成 |
| `mlxbarctl config set-adaptive-memory --enabled true` → 長い会話（≥ 発火閾値）を送信 | 応答は一貫。`/api/v1/status` の `adaptiveMemory` に `compression_ratio`、メニューバーに「約N%削減」 |
| `contextCompression.enabled` が true の状態で `adaptiveMemory.enabled true` を試みる | HTTP 422、両方は有効化できない旨（日英併記） |
| `--soft-token true` ＋ `input_embeddings` 対応の mlx-lm モデル | Hybrid Prefill が走り、`metrics` に `adaptive_memory`（`tier: soft-token`、`writer_ms` / `prefill_ms`）。応答が生成される |
| `--soft-token true` ＋ 非対応モデル（埋め込み層が見つからない / `input_embeddings` 不可） | 自動で EXACT へフォールバック。エラーにならず応答は生成される。`fallback_reason` がログ |
| Writer/Latent キャッシュのファイルを truncate してから再送 | miss として扱い、再計算。クラッシュしない |
| GUI「設定 > 詳細 > 実験的機能」 | 日本語／英語のどちらでも崩れず、方針 Picker・各 Stepper・トグル・適用が動作 |

APIトークン・リクエスト本文・個人パスは成果物に含めない。

## 6. ビルド

```sh
./scripts/build-release.sh
VERSION=2.2.0 ./scripts/verify-release.sh
shasum -a 256 dist/MLXBar-2.2.0.dmg > dist/MLXBar-2.2.0.dmg.sha256
```

DMG SHA-256: `814612bac0a1bb3b0a1293ba9a2c0145acce731cf614212c1eda972168e4137c`
