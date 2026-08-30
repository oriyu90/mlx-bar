# MLXBar v1.8.1 テスト計画と結果

対象:
1. GUI の全操作を `mlxbarctl` から実行可能にする（CLI 完全対応）
2. v1.8.0 の細部修正（Anthropic ストリームの input トークン数 / `count_tokens` エラー型 / GUI Stepper 防御）

v1.8.0 / v1.7.1 の全契約は回帰として維持する。管理 API・OpenAI 互換・Anthropic 互換の各ハンドラは
無変更。設定 schema version 1 は不変。

## 1. 自動検証

```sh
cd Coordinator && uv run pytest ../Tests -q
swift build -c debug
swift build -c release
```

2026-08-30 結果:

- Python: **360 passed**（v1.8.0 の 346 + v1.8.1 の 14：CLI 12・Anthropic 2）
- Swift Debug / Release: **成功**
- `sh scripts/build-release.sh` → `sh scripts/verify-release.sh`: **成功**（ad-hoc 署名）

## 2. CLI 完全対応の新規契約

| 契約 | 検証 |
|---|---|
| `model unload --force`（no-arg）が `DELETE /models/loaded?force=true` を送る | `test_cli::test_unload_all_forwards_force` |
| `model unload`（フラグなし）は従来どおり `DELETE /models/loaded` | `test_cli::test_unload_all_without_force_is_unchanged` |
| `model set-replicas <id> <n>` は GET+PUT のみ（ロードを起こさない）、既存プロファイルの `replicas` だけ更新 | `test_cli::test_set_replicas_updates_existing_profile_without_loading` |
| `model set-replicas` は未 pin モデルを `keepLoaded:true` で pin しつつ replicas を設定 | `test_cli::test_set_replicas_pins_an_unpinned_model` |
| `model set-replicas` は範囲外（>8）をクライアント側で拒否 | `test_cli::test_set_replicas_rejects_out_of_range` |
| `prompt-cache status` / `clear-memory` / `clear-disk` が対応エンドポイントを叩く | `test_cli::test_prompt_cache_actions_hit_the_right_endpoints` |
| `prompt-cache set` は指定フラグだけの部分パッチを送る | `test_cli::test_prompt_cache_set_builds_a_partial_patch` |
| `prompt-cache set` は不正サイズと空指定を拒否 | `test_cli::test_prompt_cache_set_rejects_bad_size_and_empty` |
| `config set-model-pool` は指定されたオプションだけ送る（`profiles` は触らない） | `test_cli::test_set_model_pool_only_sends_provided_options` |
| `config set-model-pool` は範囲外と空指定を拒否 | `test_cli::test_set_model_pool_rejects_out_of_range_and_empty` |
| `config set-flag <name>` が正しい dotted key へマップ（5 種） | `test_cli::test_set_flag_maps_names_to_dotted_keys` |
| `lmstudio set-base-url` / `set-auto-load` が `models.lmStudio.*` を送る | `test_cli::test_lmstudio_base_url_and_auto_load` |

既存 CLI テスト（`LMStudioTokenTests` / `NetworkTests` / `RuntimeCancelJobTests` / `RemoveAllDataTests`）は不変・全 PASS。

### GUI → CLI 手動確認（サービス稼働中）

```sh
mlxbarctl --json config get                    # スナップショット
mlxbarctl --json prompt-cache status
mlxbarctl prompt-cache set --disk-enabled true --max-gb 20
mlxbarctl prompt-cache clear-memory
mlxbarctl config set-model-pool --generation-concurrency 3 --max-replicas-per-model 3
mlxbarctl config set-flag anthropic-api true
mlxbarctl config set-flag remote-image-urls false
mlxbarctl model pin <id>
mlxbarctl model set-replicas <id> 2
mlxbarctl lmstudio set-base-url http://127.0.0.1:1234
mlxbarctl lmstudio set-auto-load true
mlxbarctl model unload --force
```

各コマンドの後に `mlxbarctl --json config get` / `mlxbarctl --json status` で反映を確認し、
GUI（設定画面を開き直す）で同じ値が表示されることを突き合わせる。

## 3. v1.8.0 細部修正の契約

| 契約 | 検証 |
|---|---|
| ストリームの `message_delta.usage` に実 `input_tokens` が載る（`message_start` は概算） | `test_anthropic_compat::test_streaming_message_delta_reports_the_real_input_token_count` |
| `usage` イベントを出さず `metrics.prompt_tokens` だけの Worker でも input_tokens が補正される | `test_anthropic_compat::test_metrics_only_worker_still_corrects_input_tokens` |
| `COUNT_TOKENS_UNAVAILABLE` は HTTP 503 + `error.type: api_error`（型と status を整合） | `test_anthropic_compat::test_count_tokens_returns_real_worker_count` 群 + 目視（`_anthropic_error_type`） |
| GUI Stepper が `maxReplicasPerModel <= 0` でもクラッシュしない / profiles 重複で trap しない | Swift ビルド + 目視（`MLXBarSettingsView.swift`） |
| 既存の Anthropic 非ストリーム `input_tokens`（`usage` イベント経由）は不変 | `test_non_streaming_message_shape_and_no_cache_usage_fields`（`== 11`） |

## 4. 配布検証（リリース前）

- `sh scripts/build-release.sh` → `sh scripts/verify-release.sh`。同梱 Coordinator が `version 1.8.1`、
  `GET /anthropic/v1/models` 200、`GET /v1/models` 形不変。
- `mlxbarctl --help` / `mlxbarctl config --help` / `mlxbarctl prompt-cache --help` /
  `mlxbarctl lmstudio --help` に新コマンドが出る。
- `CFBundleShortVersionString = 1.8.1` / `CFBundleVersion = 29`。
- Apple 公証: 未実施（ad-hoc 署名。`mlx-bar.md` 参照）。

## 5. 実機（要 Apple Silicon・要モデル本体、この環境では未実施）

v1.8.0 の `TEST_PLAN_v1.8.0.md` §2/§3（同一モデル並列の合算メモリトレース、Claude Code 実接続）は
v1.8.1 でも未消化のまま。CLI 追加・細部修正はいずれもその項目に影響しない。メモリに余裕のある実機で
`TEST_PLAN_v1.8.0.md` の手順を通し、あわせて上記 §2 の GUI→CLI 手動確認を実施すること。
