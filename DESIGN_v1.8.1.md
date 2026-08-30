# MLXBar v1.8.1 設計書（CLI 完全対応 / v1.8.0 の細部修正）

更新日: 2026-08-30
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

1. **GUI の全操作を `mlxbarctl` から実行可能にする。** GUI（`MenuBarViewModel` + 設定画面）が
   叩く管理 API エンドポイントに、すべて名前付き CLI サブコマンドで到達できるようにする。
2. **v1.8.0 で残った 3 つの細部を修正する。** Anthropic ストリームの input トークン数、
   `count_tokens` 非対応時のエラー型、GUI Stepper の防御。

**非目的:** 管理 API・OpenAI 互換・Anthropic 互換の各エンドポイントの挙動変更。設定 schema の変更。
CLI 追加は既存 `cli.py` への **追加のみ**（新しい subparser と `execute()` 分岐）で、
既存コマンドの挙動は変えない（唯一の例外は下記 §3 の `model unload --force` 転送で、
これも新フラグを渡したときだけ効く）。

## 2. GUI → CLI 対応調査の結果

GUI の全ミューテーション（44 メソッド + 設定トグル）を管理 API と突き合わせた。

**v1.8.0 時点で CLI から到達できなかったもの:**

| GUI 操作 | エンドポイント | v1.8.1 の対応 |
|---|---|---|
| プロンプトキャッシュ消去（メモリ / ディスク） | `POST /prompt-cache/{memory,disk}/clear` | `mlxbarctl prompt-cache clear-memory` / `clear-disk` |
| プロンプトキャッシュ統計 | `GET /prompt-cache` | `mlxbarctl prompt-cache status` |
| 全モデルアンロードの強制 | `DELETE /models/loaded?force=true` | `mlxbarctl model unload --force`（no-arg パスへ転送） |
| 複数モデル常駐フォーム（7 項目） | `PUT /settings models.pool.*` | `mlxbarctl config set-model-pool --…` |
| プロンプトキャッシュのディスク設定（2 項目） | `PUT /settings promptCache.*` | `mlxbarctl prompt-cache set --disk-enabled … --max-gb N` |
| pin 済みモデルの replicas 変更（再ロードなし） | `PUT /settings profiles[].replicas` | `mlxbarctl model set-replicas <id> <n>` |
| GUI トグル各種（下記） | `PUT /settings <dotted.key>` | `mlxbarctl config set-flag <name> {true\|false}` |
| LM Studio Base URL / 自動ロード | `PUT /settings models.lmStudio.*` | `mlxbarctl lmstudio set-base-url` / `set-auto-load` |

`config set-flag` の対象: `auto-load-on-api`（`models.autoLoadOnAPIRequest`）/
`anthropic-api`（`api.anthropic.enabled`）/ `remote-image-urls`（`security.allowRemoteImageUrls`）/
`require-token`（`api.requireToken`）/ `continue-after-gui-exit`（`general.continueAfterGUIExit`）。

**汎用エスケープハッチ:** `mlxbarctl config set <dotted.key> <json-value>` は従来どおり残し、
GUI に露出していないスカラー設定（`general.logLevel`、`promptCache.keepGenerations` 等）にも到達できる。

## 3. 不変条件（守ること）

- `cli.py` の変更は追加のみ。既存の `status` / `model *` / `generate` / `cancel*` / `runtime *` /
  `config get|set|set-*` / `secrets *` / `logs *` / `network *` / `api *` / `diagnostics` /
  `remove-all-data` の挙動は不変。
- `model unload`（no-arg・フラグなし）は従来どおり `DELETE /api/v1/models/loaded`。
  `--force` を渡したときだけ `?force=true` を付ける（`--force` 引数自体は v1.7.1 から定義済み）。
- 管理 API・OpenAI 互換・Anthropic 互換の各ハンドラは 1 行も変更しない。
  新しい CLI コマンドは既存エンドポイントの薄いラッパで、必要なエンドポイントはすべて既存。
- 設定 schema は version 1 のまま。CLI のクライアント側レンジ検証は GUI と同じ値にそろえるが、
  最終的な検証は従来どおりサーバ（`SettingsStore._validate`）が持つ。
- `config set-model-pool` は指定されたフラグのぶんだけ部分パッチを送る。`models.pool.profiles` は
  送らないので deep-merge で保持される（GUI の `setModelPoolSettings` と同じ）。

## 4. v1.8.0 の細部修正

1. **Anthropic ストリームの input トークン数**（`anthropic_stream.py`、追加のみ）
   - `handle()` の `metrics` 分岐でも `prompt_tokens` を採用する。実 Worker（mlx_vlm、
     および mlx_lm のフォールバック経路）は `usage` イベントを出さず `metrics` にだけ載せるため。
   - ストリームの `message_delta.usage` に `input_tokens` を含める（Anthropic の `message_delta` は
     累積 usage を持つ）。`message_start` は生成前の概算のままだが、`message_delta` で実数へ補正される。
2. **`COUNT_TOKENS_UNAVAILABLE` のエラー型**（`anthropic_stream._anthropic_error_type`、1 値）
   - `invalid_request_error` → `api_error`。エンドポイントが返す HTTP 503 と型を整合させる
     （「このランタイムは計測非対応」はクライアントエラーではなく能力上の制約）。
3. **GUI の防御**（`MLXBarSettingsView.swift`、2 行）
   - `Stepper(in: 1...max(1, maxReplicasPerModel))`：壊れた設定で `0` が来ても範囲構築で
     クラッシュしない。
   - `Dictionary(_:uniquingKeysWith:)`：`profiles` に万一 modelId 重複があっても trap しない。

## 5. 検証

`TEST_PLAN_v1.8.1.md` を参照。Python 回帰 **360** 件（v1.8.0 の 346 ＋ 新規 14：CLI 12・Anthropic 2）、
Swift Debug/Release ビルド成功、`build-release.sh` + `verify-release.sh` 成功。
実機 GUI／Claude Code 実接続は現場で実施（この環境に MLX ランタイム・モデル本体なし）。
