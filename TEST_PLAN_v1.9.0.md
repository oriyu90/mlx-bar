# MLXBar v1.9.0 テスト計画（モデル別の個別アンロード・生成中のモデル別トークン速度・API互換性精査）

更新日: 2026-08-31

## 0. 変更範囲の要約

- `Coordinator/mlxbar/workers/model_pool.py` `status()`: `loadedModels[]` にモデル別
  `generationTokensPerSecond` / `generatedTokens` を付与、トップレベル `generationTokensPerSecond`
  を追加（プール経路の欠落修正）。
- `Coordinator/mlxbar/api/anthropic_compat.py` `_generation_options()`: `thinking:{"type":"disabled"}`
  （budget なし）を no-op 受理。
- `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` `ResidentModel`: `generationTokensPerSecond` と
  `generationRateText(japanese:)` を追加。
- `Sources/MLXBar/MenuBar/MenuBarView.swift`: 常駐モデル一覧を常駐1つでも表示、行にトークン速度。
- `Sources/MLXBar/Resources/en.lproj/Localizable.strings`: `"生成速度"` を追加。

## 1. 自動テスト（重み不要）

```sh
Coordinator/.venv/bin/python -m pytest Tests -q      # 376 passed 目標
swift build --disable-sandbox                        # Swift 6 strict concurrency
swift build -c release --disable-sandbox
sh scripts/build-release.sh && sh scripts/verify-release.sh
```

### 1.1 新規回帰（計3件）

| テスト | 期待 |
|---|---|
| `test_model_pool.py::test_status_reports_per_model_generation_rate` | 生成中スロットの `generationTokensPerSecond` / `generatedTokens` が `loadedModels[]` の該当行に載り、他モデルの行には載らない。トップレベル `generationTokensPerSecond` に主モデルの値が反映される |
| `test_model_pool.py::test_status_has_no_generation_rate_when_idle` | アイドル時はトップレベルが `None`、行にもフィールドが無い |
| `test_anthropic_compat.py::test_thinking_disabled_is_accepted_as_a_noop` | `thinking:{"type":"disabled"}` が HTTP 200 で通り、応答本文は通常どおり |

### 1.2 既存の不変条件（無改変で緑を維持）

- `test_anthropic_compat.py` 全件（特に `test_unsupported_features_are_rejected_explicitly` が
  `thinking:{"type":"enabled","budget_tokens":1024}` を引き続き 400 にすること、
  `test_openai_surface_is_unchanged_by_the_anthropic_mount`）。
- `test_openai_tools.py` 全件（`finish_reason`、`delta.role` 一回、`stream_options`、tool call 分割）。
- `test_model_pool.py` / `test_model_replicas.py` の既存 status / unload / replica テスト。
- `test_cli.py` の `model unload` サブコマンド。

### 1.3 既知のフレーク

- `test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats`:
  `heartbeat_interval_seconds: 0.03` が並列フル実行時の負荷に敏感。**単体実行では安定して成功**
  （`pytest Tests/test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats`）。
  フル実行で失敗した場合は単体で再実行して確認する。互換性・機能とは無関係。

## 2. 手動確認 — 次に Apple Silicon 実機を触る人がやること

前提: 小型モデル2つ（例 `Qwen3.5-9B-MLX-8bit` と別の mlx-lm モデル）を `keepLoaded` で常駐、
`models.pool.enabled: true`、`maxResidentModels: 2` 以上。

### 2.1 モデル別トークン速度

1. 2モデル常駐の状態でメニューバーを開く → 「常駐モデル」一覧に2行。生成が無いので速度行は出ない。
2. 片方のモデルへ長い `stream:true` 生成を投入（`curl -N` で OpenAI 経路、または ZCode / Claude Code）。
   → そのモデルの行の下に「N tok/秒」（英語 UI では「N tok/s」）が出る。**もう片方の行には出ない。**
   ヘッダーの「モデルが応答を生成中」下の速度と、生成中モデルの行の速度が一致する。
3. 2モデルへ同時に長い生成を投入 → 両方の行にそれぞれの速度が出る。値は毎秒更新され、
   10 未満は小数1桁、10 以上は整数。メニューバーアイコンがちらつかないこと。
4. 生成が終わると速度行が消える。
5. `replicas: 2` のモデルを1つ常駐し、2本同時生成 → 畳まれた1行（`×2` バッジ付き）に、
   速いほうのレプリカの現在値が出る。
6. `models.pool.enabled: false` に変更してサービス再起動 → 単一モデルのヘッダー速度が
   従来どおり出る（`_legacy` 経路の回帰。v1.9.0 でトップレベル `generationTokensPerSecond` を
   プール経路にも追加したが、無効時は `_legacy.status()` そのままで無変更）。

### 2.2 モデルの個別アンロード

1. 常駐が **1つ** の状態でメニューバーを開く → 「常駐モデル」一覧が1行表示される（v1.8.4 までは
   非表示だった）。行の eject ボタンでそのモデルを解放できる。ヘッダーの「アンロード」でも解放できる
   （どちらでも同じ結果）。
2. 2モデル常駐 → 一方の行の eject → そのモデルだけ解放され、もう一方は常駐したまま。解放したモデルへ
   の API 要求は `autoLoadOnAPIRequest` に従って再ロードされる。
3. 一方のモデルが生成中に、もう一方（アイドル）の行の eject → 成功。生成中モデルには影響しない。
4. **生成中のモデル**の行の eject → `ENGINE_BUSY`（日本語で表示）。English UI で英語表示。
   `mlxbarctl model unload <id> --force` は当該モデルの生成のみキャンセルして解放し、他モデルは継続。
5. ピン（pin）トグルは従来どおり。LM Studio 管理モデルの行には eject / pin を出さない。

### 2.3 API 互換性（実機スモーク）

1. **Claude Code**: `ANTHROPIC_BASE_URL=http://127.0.0.1:11435/anthropic`、
   `ANTHROPIC_AUTH_TOKEN=<control/api-token>`、`ANTHROPIC_MODEL=<GET /anthropic/v1/models の名前>`。
   チャット / tool use / `/model` 切替 / 中断・リトライ / 並列 subagent / `count_tokens` が動く。
   拡張思考を **オフ**（既定）のまま使う分には 400 が出ない。`thinking:disabled` を送るクライアント
   （一部の Anthropic SDK ラッパ）でも 400 にならない。
2. **OpenAI / ZCode**: `stream:true` の tool calling、`max_tokens` 省略で応答が 512 で切れない、
   `finish_reason=length` が伝わる、`/v1/completions` が OpenAI エラー形。
3. Claude Code と OpenAI クライアントを同時接続して相互ブロックが無いこと（別モデル指定）。

### 2.4 持ち越し（v1.8.0 由来、v1.9.0 でも未消化）

- `TEST_PLAN_v1.8.0.md §2`（レプリカ実機の合算メモリトレース）、`§3`（Claude Code 実接続の詳細）。
  v1.9.0 はスケジューリング／メモリガードに触れていないため、この項目はそのまま持ち越し。
