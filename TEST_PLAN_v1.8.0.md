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
- `sh scripts/build-release.sh` → `sh scripts/verify-release.sh`: **成功**（ad-hoc 署名）。
  同梱 Coordinator で `version 1.8.0` / `/anthropic/v1/models` 応答 / `/v1/models` 形不変 /
  `anthropic-version` 欠落 → 400 / `profiles[].replicas=9` → 422 を確認。
- 実機一部実施（§2・§3 に詳細）: `_admit` メモリ圧ガード発火、replica 0 ロード + 自前予約 +
  素の manifest、replica 1 の admission 拒否で「非致命」経路、Anthropic 非ストリーム／ストリーム／
  `stop_sequence`／`count_tokens`／OpenAI 経路無影響 を実 MLX モデルで確認。

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

### 2026-08-30 実機一部実施（M-series / 96 GB / mlx-lm 0.31.3 / `Qwen3.5-9B-MLX-8bit`）

隔離した `--home`（別 UDS ソケット・runtimes は既存 slot を symlink・throwaway）で dev coordinator を
起動して確認した。**実施できた項目:**

- **`_admit` の macOS メモリ圧ガードが実機で end-to-end に発火。** 当時マシンは
  `kern.memorystatus_vm_pressure_level = 2`（inactive 約 17 GB あるが warning）で、150 MB のモデルすら
  `MEMORY_PRESSURE` で拒否。設計どおり（サイズ非依存の保守的ガード）。
- 計測用に報告 pressure を 1 にクランプした状態で **replica 0 がロード**、`set_memory_limit`
  ≈ 13.6 GB の自前予約を取得。manifest は素の `worker-<digest>.json`（`-index` サフィックス無し
  ＝ v1.7.1 と同一）。
- **replica 1 は admission が拒否**（replica 0 が空きメモリを消費し、実空きが `estimate + reserve`
  に届かず）→ `Could not load replica 1 ... keeping 1 replica(s)` をログし、**replica 0 は `ready`
  のまま利用可能**。「2 個目以降の失敗は致命的でない」経路が実機で機能。
- **OpenAI 経路が同一 coordinator 上で無影響**（`Qwen3.5-9B` で `object: chat.completion` /
  `finish_reason: stop`）。OpenAI の 422 は `INVALID_REQUEST` / `invalid_request_error` のまま
  （Anthropic エンベロープにならない）。

**実施できなかった項目（このマシンの当時のメモリ状況では 10 GB×2 の同時常駐に必要な約 30 GB の
実空きが取れなかった。バグではない）:**

- 2 レプリカが同時にストリームし `activeGenerations=2` になること、その間の RSS 合計 /
  `vm_stat` / `kern.memorystatus_vm_pressure_level` の 1 秒間隔トレース。
- `generationConcurrency=1` 再起動で複数レプリカでも完全直列になること（実機）。

ルーティング／並行度そのものは fake worker のテスト（`test_model_replicas`）で担保済み。
合算 allocation ピークの妥当性が実機で確認できるまで `replicas > 1` の実機保証はしない
（v1.7.0 の `generationConcurrency` 実機項目と同じ扱い）。**メモリに余裕のある実機で下記 1–5 を
通し、結果をここに追記すること。**

> 注意: dev coordinator の `api.port` は既存の MLXBar アプリと衝突しないポートに変えること
> （`SO_REUSEADDR` のため 11435 のまま起動すると衝突が silent になり、動作中のアプリの
> ポート提供を一時的に奪う）。2026-08-30 の確認ではこの点を踏み、以後は別ポート推奨。

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

### 2026-08-30 実機実施（`Qwen3.5-9B-MLX-8bit`、上記 §2 と同じ隔離 coordinator）

- `GET /anthropic/v1/models` → Anthropic 形（`data[].type=model` / `display_name` / `has_more:false`
  / `first_id` / `last_id`）。認証は `x-api-key` で通過。
- **非ストリーム `POST /anthropic/v1/messages`**: `{"type":"message","role":"assistant",
  "model":"Qwen3.5-9B-MLX-8bit","content":[{"type":"text","text":"pong"}],
  "stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":17,"output_tokens":2}}`。
  cache 系フィールド無し、`model` は実ローカル名。
- **ストリーム**: イベント列 `message_start → ping → content_block_start →
  content_block_delta ×N → content_block_stop → message_delta → message_stop`。`[DONE]` 無し。
- **`stop_sequences:["3"]`** → `stop_reason:"stop_sequence"`, `stop_sequence:"3"`, 本文は "3" の
  手前で切れる（`"...1, 2, "`）。
- **`count_tokens`**: 実トークナイザで `{"input_tokens":22}`（文をチャットテンプレートで包んだ値）。
- `anthropic-version` ヘッダ欠落 → 400（Anthropic 形）。`api.anthropic.enabled=false` で
  `/anthropic/*` → 404（パッケージ済み Coordinator でも確認、§4）。

**未実施:** Claude Code 本体を `ANTHROPIC_BASE_URL` で繋いだ対話（tool use / `/model` / 中断 /
並列 subagent）。下記手順で実施すること。

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
