# MLXBar v1.7.0 テスト計画と結果

対象: モデル間同時生成（`models.pool.generationConcurrency`、既定2）、メモリ・ヘッドルームガード、
モデル別キュー/キャンセル/孤児レーン回復、個別モデル unload、常駐プロファイルの GUI/CLI 露出。
v1.6.2 の全契約は回帰として維持する。

## 1. 自動検証

```sh
Coordinator/.venv/bin/python -m pytest Tests -q
swift build --disable-sandbox -c debug
swift build --disable-sandbox -c release
```

2026-08-28 結果:

- Python: **307 passed**（v1.6.2 の 289 + v1.7.0 の 18）
- Swift Debug: **成功**
- Swift Release: **成功**

v1.7.0 の新規契約:

| 契約 | 検証 |
|---|---|
| 別モデルは同時生成の上限まで並行 | `test_distinct_models_generate_concurrently` |
| 同一モデルへの要求は直列のまま | `test_same_model_requests_stay_serialised` |
| `generationConcurrency=1` は全体で1件（旧挙動） | `test_concurrency_one_serialises_across_models` |
| 既定 `generationConcurrency` は 2 | `test_default_generation_concurrency_is_two` |
| メモリガード発火時は失敗せずキューへ降格 | `test_memory_guard_downgrades_second_lane_to_queue_without_failing` |
| 孤児レーンを owner 消失後に回復 | `test_orphaned_lane_is_recovered` |
| 待機中要求をモデル単位でキャンセル | `test_queued_request_can_be_cancelled_per_model` |
| キュー待ち中の client 切断でレーンを漏らさない | `test_client_disconnect_while_queued_leaves_no_leaked_lane` |
| 24 並行＋途中キャンセルでも lane/permit/lease を漏らさない | `test_stress_concurrent_requests_leave_no_leaked_lane_state` |
| 負荷時も同時生成が設定上限を超えない | `test_concurrency_never_exceeds_the_configured_limit_under_load` |
| status に同時生成数/上限を出す | `test_status_reports_concurrency_and_active_lanes` |
| 1モデルだけ unload、他は常駐維持 | `test_unload_one_model_leaves_the_other_resident` |
| lease 中の個別 unload は `ENGINE_BUSY` | `test_unload_one_model_refuses_while_leased` |
| 既定2・範囲1–8・headroom の検証 | `test_pool_generation_concurrency_default_and_range` |
| 2常駐モデルへの同時ストリームが API 層で直列化されない | `test_two_resident_models_stream_without_api_layer_serialisation` |
| 実プール経由の OpenAI API で2モデルが同時ストリーム | `test_real_pool_behind_openai_api_streams_two_models_concurrently` |
| 要求は指定した常駐モデルへルーティング | `test_chat_completions_route_to_the_requested_resident_model` |
| tools 配列の上限は128（129は422） | `test_tool_list_boundary_is_128_entries` |

## 2. 実機メモリ検証（要 Apple Silicon 実機・要モデル本体）

**このビルド環境には MLX ランタイムもモデル本体も無いため未実施。以下はリリース前に現場で通すこと。**
合算 allocation ピークの妥当性が確認できるまで、`generationConcurrency > 1` の実機保証はしない
（v1.6.0 / v1.6.2 の実機項目と同じ扱い）。

1. `Qwen3.5-9B-MLX-8bit` と小型 mlx-lm モデルの2つを `POST /api/v1/models/{id}/load` で常駐させる
   （または `mlxbarctl model pin` の後にサービス再起動でプリロード）。`mlxbarctl model resident` で
   `residentCount=2`、`generationConcurrency=2` を確認。
2. 2つの `curl -N`（`Authorization: Bearer $(cat ~/Library/Application\ Support/MLXBar/control/api-token)`）で
   それぞれのモデルへ `stream:true` の長い生成を同時に投げる。両方の SSE が同時に token を返し、
   `GET /api/v1/status` の `activeGenerations` が 2、各 `loadedModels[].laneQueueDepth` が 0 であること。
3. 同時生成中の 1 秒間隔で以下を記録:
   - `/bin/ps -o rss= -p <workerA_pid> <workerB_pid>` と Coordinator の RSS 合計
   - `/usr/bin/vm_stat` の free/inactive
   - `/usr/sbin/sysctl -n kern.memorystatus_vm_pressure_level`（0 のままであること。2 以上になったら
     `perGenerationHeadroomGB` を上げるか `generationConcurrency=1` へ戻す判断材料）
4. 同一モデルへ 2 件同時に投げ、2 件目が `queue` イベントを受けてから直列に処理されること。
5. `generationConcurrency=1` に設定してサービス再起動し、2. が完全に直列（片方完了まで他方は
   `queue` のまま）になること = v1.6.2 挙動の回帰。
6. 片方の生成中に `POST /api/v1/models/{そのモデル}/unload` が `ENGINE_BUSY`、
   もう片方（idle）の unload は成功し、生成中の側は継続すること。
7. 生成中クライアントを切断し、`generationLockRecoveries` が増えるか、次要求が正常に開始すること
   （孤児レーンが残らない）。

## 3. OpenClaw 連携検証（要 OpenClaw 常駐）

`openclaw.md §3` に従い、`~/.openclaw/openclaw.json` の
`models.providers.mlxbar.timeoutSeconds` を明示設定してから実施する（無設定だと暗黙 ~120 秒で
先に切れる）。

1. MLXBar に 2 モデルを常駐させ、OpenClaw の 2 つのエージェント（または 2 つの cron）を
   **別々のモデル ID** で設定する。両方を同時実行し、一方の生成がもう一方をブロックしないこと。
2. `tools` を 128 件近くまで積んだエージェントで、生成前 422（`toolsは最大128件の配列です`）が
   出ないこと。129 件で 422 が即時（20–30 ms）に返ること。
3. モデル ID は `curl .../v1/models` の表示名（例 `Qwen3.8-27B-MLX-6bit`）を使うこと。
   LM Studio 内部パス表記とは別物（`mlx-bar.md §0-A`）。
4. 主エージェント（mlx-bar）とフォールバック（LM Studio）の切替が従来どおり動くこと。

## 4. 配布物検証

- `scripts/build-release.sh` で arm64 app と `dist/MLXBar-1.7.0.dmg` を生成。
- `scripts/verify-release.sh` で構成ファイル、`plutil -lint`、`codesign --verify --deep --strict`、
  `__pycache__` 非収録、`hdiutil verify` を確認。
- DMG の SHA-256 を `RELEASE_NOTES_v1.7.0.md` と `.sha256` へ記録。

2026-08-28 結果（ad-hoc 署名）:

- `build-release.sh`: **成功**（PyInstaller ×2 → app bundle → ad-hoc codesign →
  `codesign --verify --deep --strict` 合格 → DMG 生成 → `hdiutil verify` VALID）。
- `verify-release.sh`: **成功**（全 `test`、`plutil -lint` ×2、署名検証、bytecode 非収録、
  `hdiutil verify` すべて合格、exit 0）。
- 同梱 Coordinator の起動確認: `MLXBar.app/Contents/Resources/coordinator/MLXBarCoordinator
  --home <tmp>` を起動し `GET /api/v1/health` → `{"status":"ok","version":"1.7.0"}`。
- パッケージ済みバイナリでの新契約スモーク:
  - `/api/v1/status` の `modelPool.generationConcurrency=2` / `activeGenerations=0` /
    `generationLockRecoveries=0`。
  - `PUT /api/v1/settings` `generationConcurrency=3` 受理、`=9` は 422 `INVALID_SETTINGS`、
    `perGenerationHeadroomGB=0.1` は 422。
  - `POST /api/v1/models/<不明>/unload` → 200 `{"state":"unloaded","count":0}`（新ルート登録確認）。
  - `/v1/models` 認証なし → 401 `AUTHENTICATION_FAILED`、トークンあり → 200 カタログ。
  - Chat Completions `tools` 128 件は受理（モデル解決で 404）、129 件で 422
    `toolsは最大128件の配列です`。

DMG SHA-256 は `RELEASE_NOTES_v1.7.0.md` と `dist/MLXBar-1.7.0.dmg.sha256` に記載。
Apple 公証は未対応（ad-hoc 署名。`mlx-bar.md §5` の既知事項）。
