# MLXBar v1.8.0 テスト計画と結果

対象:
1. 同一モデルの並列常駐&生成（`models.pool.profiles[].replicas`、`maxReplicasPerModel`）
2. Anthropic 互換 API（`/anthropic/v1/messages` ほか、Claude Code 対応）

v1.7.1 の全契約は回帰として維持する。`generationConcurrency==1` ≡ v1.6.2、`pool.enabled=false` ≡
v1.6.1、OpenAI 互換入口は不変（`openai_compat.py` 無変更）。

## 1. 自動検証

```sh
cd Coordinator && uv run pytest ../Tests -q
swift build --disable-sandbox -c debug
swift build --disable-sandbox -c release
```

2026-08-30 結果:

- Python: **346 passed**（v1.7.1 の 316 + v1.8.0 の 30）
- Swift Debug / Release: **成功**

v1.8.0 の新規契約:

| 契約 | 検証 |
|---|---|
| `replicas` 既定 1 はスロットキーが bare model_id（v1.7.1 等価） | `test_model_replicas::test_default_replicas_is_one_and_keeps_the_bare_slot_key` ＋ 既存 316 件 |
| pinned `replicas=N` で N 個の独立 Worker（instance_key `-index` 分離） | `test_pinned_replicas_load_independent_workers` |
| `replicas` は `maxReplicasPerModel` で clamp | `test_replica_count_is_clamped_to_max_replicas_per_model` |
| admission は per-replica で charge、予算超過を拒否 | `test_admission_charges_per_replica_and_rejects_over_budget` |
| 同一モデルの並行要求が別レプリカへ振り分けられる | `test_concurrent_same_model_requests_use_distinct_replicas` |
| `generationConcurrency=1` は複数レプリカでも直列 | `test_generation_concurrency_one_serialises_even_with_replicas` |
| `unload_model` が全レプリカ解放 | `test_unload_model_frees_every_replica` |
| status に `replicaIndex` / `replicaCount` / `maxReplicasPerModel` | `test_status_reports_replica_index_and_count` |
| `replicas` を下げると reaper が超過レプリカを LRU evict | `test_reaper_scales_replicas_down_when_config_lowered` |
| `pool.enabled=false` はレプリカを無視（legacy 経路） | `test_pool_disabled_ignores_replicas` |
| Anthropic 非ストリーム message 形、`usage` に cache フィールドなし | `test_anthropic_compat::test_non_streaming_message_shape_and_no_cache_usage_fields` |
| system / text / image / tools / tool_choice / tool_result の変換 | `test_system_and_text_blocks...`, `test_tool_use_and_tool_result...`, `test_tools_and_tool_choice_variants...`, `test_image_block_reaches_the_private_workspace` |
| ストリームは `[DONE]` なし、`message_start`→…→`message_stop` | `test_streaming_event_sequence_has_no_done_and_ends_with_message_stop` |
| tool_use ストリームは `input_json_delta` + `stop_reason: tool_use` | `test_streaming_tool_use_uses_input_json_delta_and_tool_use_stop_reason` |
| stop_reason マッピング（stop_sequence / max_tokens ほか） | `test_stop_sequence_finish_maps_to_stop_reason_stop_sequence`, `test_length_finish_maps_to_max_tokens` |
| 認証は x-api-key と bearer 両方、`anthropic-version` 必須、`request-id` ヘッダ | `test_x_api_key_and_bearer_are_both_accepted`, `test_missing_anthropic_version_header_is_rejected` |
| 未対応機能（thinking / server tool / document block）は明示 400 | `test_unsupported_features_are_rejected_explicitly` |
| `api.anthropic.enabled=false` で `/anthropic/*` が 404 | `test_disabled_flag_makes_anthropic_paths_404` |
| `count_tokens` は Worker の実カウント、未対応ランタイムは 501 | `test_anthropic_compat::test_count_tokens_returns_real_worker_count`, `test_worker_server::test_count_tokens_rpc_*` |
| `GET /anthropic/v1/models` は Anthropic 形、未知は not_found_error | `test_models_listing_is_in_anthropic_shape` |
| OpenAI 経路が Anthropic 追加後も不変・同じ生成基盤を共有 | `test_openai_surface_is_unchanged_by_the_anthropic_mount`, `test_openai_and_anthropic_requests_share_the_same_generation_backend` |

## 2. 手動: 同一モデルの並列（要 Apple Silicon 実機・要モデル本体）

**このビルド環境には MLX ランタイムもモデル本体も無いため未実施。リリース前に現場で通すこと。**
合算 allocation ピークの妥当性が確認できるまで `replicas > 1` の実機保証はしない（v1.7.0 の
`generationConcurrency` 実機項目と同じ扱い）。

1. 小型 mlx-lm モデルを `models.pool.profiles` に `{"modelId": ..., "keepLoaded": true, "replicas": 2}`
   で登録 → サービス再起動 → `mlxbarctl model resident` で `replicaCount=2`、`/bin/ps` で 2 Worker プロセス。
2. 同一モデルへ `curl -N` の `stream:true` を 2 本同時投入 → 両方が同時に token を返す
   （`/api/v1/status` の `activeGenerations=2`、`loadedModels` に replicaIndex 0/1 それぞれ活動）。3 本目はキュー。
3. `models.pool.generationConcurrency` を 1 にして再起動 → 同一モデルでも完全直列（v1.7.x 回帰）。
4. 2 モデル常駐 + 一方 replicas=2、同時ストリーム中に 1 秒間隔で RSS 合計 /
   `/usr/bin/vm_stat` / `sysctl -n kern.memorystatus_vm_pressure_level` を記録。pressure が 2 以上へ
   上がるなら `perGenerationHeadroomGB` を上げるか `generationConcurrency`／`replicas` を下げる。
5. 生成中の 1 レプリカへ `POST /api/v1/models/{id}/unload?force=true` → そのモデルの生成のみ停止、
   他モデルは継続。生成中クライアント切断 → `laneRecoveries` 増加または次要求が正常開始。

## 3. 手動: Anthropic / Claude Code（要実機）

1. `curl -s -H "x-api-key: $(cat ~/Library/Application\ Support/MLXBar/control/api-token)" \
   -H "anthropic-version: 2023-06-01" http://127.0.0.1:11435/anthropic/v1/models` → モデル一覧。
2. Claude Code:
   ```sh
   export ANTHROPIC_BASE_URL=http://127.0.0.1:11435/anthropic
   export ANTHROPIC_AUTH_TOKEN=$(cat ~/Library/Application\ Support/MLXBar/control/api-token)
   export ANTHROPIC_MODEL=<GET /anthropic/v1/models が返すローカルモデル名>
   claude
   ```
   チャット / tool use（Read・Bash・Edit 等）/ `/model` / 中断（Esc）/ リトライ / 並列 subagent /
   長文コンテキストでの `count_tokens` を確認。
3. Zed 等の OpenAI クライアントと Claude Code を同時接続 → キュー・複数モデル・切断で相互ブロックしない。
4. `x-api-key` を誤った値にして 401 が Anthropic 形（`{"type":"error","error":{"type":"authentication_error"}}`）。
5. `api.anthropic.enabled` を false にして再起動 → `/anthropic/*` が 404、OpenAI 経路は無影響。

## 4. 配布検証（リリース前）

- `sh scripts/build-release.sh` → `sh scripts/verify-release.sh`。同梱 Coordinator が `version 1.8.0`、
  `GET /anthropic/v1/models` 200、`GET /v1/models` 形不変、`profiles[].replicas` バリデーション（0/9 で 422）。
- `CFBundleShortVersionString = 1.8.0` / `CFBundleVersion = 28`。
- Apple 公証: 未実施（ad-hoc 署名。`mlx-bar.md` 参照）。
