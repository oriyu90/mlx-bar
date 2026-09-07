# MLXBar v2.0.1 テスト計画と結果

対象: `maxResidentModels` が同一モデルの追加レプリカも数えてしまい `replicas` が
1体常駐時に効かない不具合の修正（`_admit` / `_reap_once`）と、レプリカ不足・再起動待ちの
診断表示の追加。詳細は `DESIGN_v2.0.1.md`、調査は `INVESTIGATION_v2.0.0_2026-09-08.md`。

## 1. 自動検証（Python）

```sh
cd Coordinator && uv run pytest ../Tests -q -p no:randomly
```

2026-09-08 結果:

- **410 passed**（v2.0.0の406 + 新規4）
- ランダム順（`-p no:randomly` なし）でも 410 passed
- 既知フレーク `test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats` は
  フル並列で時々落ちるが単体・直列は常に緑（§0-S・§0-R で既記録、据え置き）。今回の連続実行では再現せず。

## 2. 本リリースの新規契約

すべて `Tests/test_model_replicas.py` に追加。

| # | 契約 | 検証 |
|---|---|---|
| 1 | `maxResidentModels = 1` でも、メモリに余裕があれば同一モデルの2体目のレプリカがロードされる | `test_replicas_load_even_when_max_resident_models_is_one` |
| 2 | `maxResidentModels = 1` のとき、2種類目の異なるモデルは従来どおり `MEMORY_BUDGET_EXCEEDED` で拒否される | `test_max_resident_models_one_still_rejects_a_second_distinct_model` |
| 3 | `maxResidentModels = 1` でも、2レプリカ分のメモリ予算が無ければ1体だけで安全に稼働し、`modelPool.replicaShortfalls` と `loadedModels[].desiredReplicaCount` / `readyReplicaCount` に不足が現れる | `test_second_replica_still_gated_by_memory_budget_when_models_cap_is_one` |
| 4 | `modelPool` が `configuredGenerationConcurrency` / `effectiveGenerationConcurrency` を併記し、保存済み値が実効値と異なると `restartRequired` が true になる | `test_status_reports_configured_vs_effective_generation_concurrency` |

## 3. 回帰

- `test_model_pool.py`（複数モデルプールの入場制御・LRU退避・TTL・メモリ圧・実行時引き下げ）
  の既存全件が無改変で緑。特に:
  - `test_third_model_evicts_oldest_idle_api_model`（`maxResidentModels=2` で3種類目が最古を退避）
  - `test_manual_pin_is_never_lru_evicted`
  - `test_live_resident_reduction_evicts_oldest_unpinned_model`（実行時引き下げ）
  - `test_post_load_measurement_cannot_exceed_the_admitted_reservation`（ロード後実測のメモリ拒否）
- `test_model_replicas.py` の既存全件が緑（`replicas == 1` は v1.7.x とバイト等価、
  レプリカ単位のメモリ課金、`maxReplicasPerModel` クランプ、`generationConcurrency = 1` の直列化、
  reaper のレプリカ縮退、`enabled = false` はレプリカ無視）。
- 設定スキーマ（`SettingsStore.DEFAULTS`）・Coordinator/Worker間RPC・OpenAI/Anthropic/管理APIの
  ハンドラは無変更。追加した `modelPool` / `loadedModels[]` フィールドはすべて増分で、
  既存キーの削除・改名・型変更なし。既存 `config.json` はそのまま読める。

## 4. Swiftビルド

```sh
swift build --disable-sandbox
```

2026-09-08: 成功（Swift 6 strict concurrency）。変更は `MenuBarViewModel`（`modelPool` から
`restartRequired` と `replicaShortfalls` を読む2つの `@Published` を追加）と
`MLXBarSettingsView`（該当時のみ表示する注意書き＋「レプリカ不足」行）、
`en.lproj/Localizable.strings` に2キー追加。日本語はソース文字列。

## 5. 実機確認（Apple Silicon）

`/Applications/MLXBar.app` を v2.0.1 へ入れ替え、coordinator 再起動後に確認する:

| ケース | 期待 |
|---|---|
| `/api/v1/health` | `{"status":"ok","version":"2.0.1"}` |
| `maxResidentModels = 1` ＋ 固定プロファイル `replicas = 2`（メモリ余裕あり） | 同一モデルのWorkerが2体常駐（`mlxbarctl models resident` で `replicaCount:2` / `readyReplicaCount:2`） |
| 上記状態で同時2要求 | 2要求が別レプリカへ振られ、`generationConcurrency` の上限内で並行生成 |
| 2レプリカ分のメモリが無い環境 | 1体で安全稼働、`mlxbarctl models resident` の `replicaShortfalls` に `desired:2 / ready:1 / code` |
| `generationConcurrency` を保存だけして未再起動 | `mlxbarctl models resident` の `restartRequired:true`、GUI「設定 > モデル」に注意書き |
| 新パラメータ無指定の通常のOpenAI/Anthropicリクエスト | 回帰なし |

APIトークン・リクエスト本文・個人パスは成果物に含めない。

## 6. ビルド

```sh
./scripts/build-release.sh
VERSION=2.0.1 ./scripts/verify-release.sh
shasum -a 256 dist/MLXBar-2.0.1.dmg > dist/MLXBar-2.0.1.dmg.sha256
```

DMG SHA-256: `fcc36c0753881191c5a73a1ed061ca1fb1426f75662bdad04b082f20e664a06a`
