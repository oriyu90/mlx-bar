# MLXBar v1.9.0 設計書（モデル別の個別アンロード・生成中のモデル別トークン速度表示・API互換性精査）

更新日: 2026-08-31
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:**

1. **常駐モデルを1つずつアンロードできるようにする。** バックエンド（`POST /api/v1/models/{id}/unload`、
   `mlxbarctl model unload <id> [--force]`）は v1.7.0 / v1.8.1 で実装済みだが、メニューバーの
   常駐モデル一覧（＝行ごとの eject / pin）は **複数常駐時だけ** 表示していた。常駐が1つでも
   一覧を表示し、どの構成でも1モデル単位で解放できるようにする。
2. **複数モデルを常駐させているとき、生成中の各モデルのトークン毎秒を、そのモデルの行の下に表示する。**
   単一モデルのヘッダー表示（`generationTokensPerSecond`）は既にあるが、常駐モデル一覧の各行には
   速度が出ていなかった。
3. **Anthropic互換 / OpenAI互換 / ZCode（OpenAI互換のコーディングクライアント）の互換性を精査し、
   問題を洗い出す。** 精査の結果として安全な範囲の改善を1件（`thinking:disabled` の no-op 受理）
   だけ取り込む。

**非目的:**

- 生成スケジューリング・メモリガード・プール入場制御は無変更。`generationConcurrency`、
  `maxResidentModels`、`replicas` の意味は不変。
- Coordinator の OpenAI互換 / Anthropic互換の **成功経路の応答ボディ・`usage`・エラー形・
  `code` 値** は不変。追加は `/api/v1/status`（管理API、GUI専用）のフィールドのみ。
- 設定 schema（version 1）は不変。
- `POST /api/v1/models/{id}/unload` と CLI は無変更。GUI の表示条件を広げるだけ。
- Apple 公証は引き続き未対応（ad-hoc 署名）。

## 2. 変更内容

### 2.1 `Coordinator/mlxbar/workers/model_pool.py` — `status()` にモデル別トークン速度

プール有効時の `status()` は各スロットの `slot.worker.status()`（`WorkerSupervisor.status()`）を
集約している。`WorkerSupervisor.status()` は生成中なら `**self._live_generation()` により
`generationTokensPerSecond` / `generatedTokens` を **既に** 返しているが、プール側は
`child["loadedModel"]` しか取り出しておらず、この2フィールドを `loadedModels[]` の各要素へ
転記していなかった。

- 各スロットの `loaded_models` エントリに、`child.get("generationTokensPerSecond")` が
  正の数のときだけ `generationTokensPerSecond`（`round(x, 1)`）と、あれば `generatedTokens` を付与。
- **同一モデルの複数レプリカ**は GUI 側で1行に畳まれる（`×N` バッジ）。畳んだ行が「たまたま
  最初にソートされたレプリカ」の値になるのを避けるため、`status()` 内で model id ごとに
  レプリカ横断の最大値を求め、その model id の全エントリへ同じ値を書き戻す。
- トップレベル `generationTokensPerSecond` を追加。主モデル（`primary_descriptor`）の値、
  無ければ「生成中のいずれかのモデル」の値。**これは既存の不具合修正でもある**: 単一Worker経路
  （`_legacy.status()`）は `_live_generation()` 経由でこのフィールドを出すのに、プール経路
  （既定で有効）は一度も出しておらず、`MenuBarViewModel.generationRateText` が常に nil だった。

fail-safe: いずれも「値が無ければフィールドを出さない / None」。欠損で例外にしない。
`self.enabled == False`（プール無効）経路は `_legacy.status()` をそのまま返すため無変更。

### 2.2 `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` — `ResidentModel`

- `generationTokensPerSecond: Double?` を追加（`loadedModels[].generationTokensPerSecond` を
  `NSNumber` から読む。0 以下は nil）。
- `func generationRateText(japanese:) -> String?` を追加。既存のヘッダー用 `generationRateText`
  と同じ丸めルール（10 tok/秒以上は整数、未満は小数1桁。`MenuBarExtra` の
  `.menuBarExtraStyle(.window)` が高頻度の内容変化で不安定になる既知事項への対策）。
- `ResidentModel` は `Hashable`。フィールド追加で `setIfChanged(\.residentModels, …)` は
  生成中に毎秒 re-publish しうるが、対象は **ポップオーバー内の一覧のみ**。メニューバーの
  アイコン／`shortStatus` は `residentModels` を参照しないため、v1.2.1 で問題になった
  アイコンのちらつきには影響しない。値はサーバ側で 0.1 に丸め済みなので、速度が実際に
  0.1 動いたときだけ変化する。

### 2.3 `Sources/MLXBar/MenuBar/MenuBarView.swift`

- 常駐モデル一覧の表示条件を `model.residentModels.count > 1` → `!model.residentModels.isEmpty`。
  見出しの「· N」件数は複数常駐時のみ付ける。
- `residentRow` の名前・詳細の下に、`resident.generationRateText(japanese:)` が非 nil のとき
  だけ「N tok/秒」（英語 UI では「N tok/s」）を `caption2` / `monospacedDigit` / secondary で表示。
  `accessibilityLabel` は `LS("生成速度") + " " + rate`。
- 既存のヘッダー「アンロード / すべてアンロード」ボタンと、行ごとの eject / pin は不変。
  行の eject は `.disabled(model.busy)`（GUI の別操作中のみ無効。別モデルがAPI経由で生成中でも
  アイドルなモデルは解放できる）。

### 2.4 `Sources/MLXBar/Resources/en.lproj/Localizable.strings`

- `"生成速度" = "Generation speed";` を追加（accessibility ラベル）。日本語 UI は日本語リテラルが
  そのままキーなので `ja.lproj` への追加は不要（`ja.lproj` は「source language は日本語」方針で
  意図的に最小）。

### 2.5 `Coordinator/mlxbar/api/anthropic_compat.py` — `thinking:{"type":"disabled"}` の no-op 受理

`_generation_options` は `body.get("thinking") is not None` で一律 HTTP 400 にしていた。
`{"type":"disabled"}`（`budget_tokens` なし）は「拡張思考を使わない」という指定で、ローカルモデルは
もともとその挙動。これを no-op として受理する（`thinking = None` に潰してから既存の判定へ）。

- 拡張思考の **有効化**（`type` が `enabled` / `adaptive`、または `budget_tokens` あり）は
  従来どおり 400 `invalid_request_error`。
- サーバーサイドツール、`document` ブロック、Anthropic側 MCP も従来どおり 400。
- **成功経路の応答・`usage`・エラー形は一切変わらない。** 変わるのは「`thinking:disabled` を
  送るクライアントが 400 で弾かれなくなる」ことだけ。

## 3. API互換性の精査（結果）

### 3.1 Anthropic互換（`/anthropic`、Claude Code 向け）

| 項目 | 状態 |
|---|---|
| 認証（`x-api-key` / `Authorization: Bearer`）、`anthropic-version` 必須、401/400 の Anthropic エラー封筒 + `request-id` | ✅ `PublicRequestGuard`（ASGI 層）で早期判定。自動テスト済み |
| SSE: `message_start`→`content_block_start`→`content_block_delta`(`text_delta`/`input_json_delta`)→`content_block_stop`→`message_delta`→`message_stop`、`ping`、`event: error`（後続に `message_stop` を出さない） | ✅ `anthropic_stream.py`。`[DONE]` は出さない |
| `system`（文字列／テキストブロック配列）、`tool_use`／`tool_result` ブロックの内部トランスクリプトへの相互変換、画像 source（base64／url） | ✅ 変換して同じ worker 経路へ |
| `stop_reason` マッピング（`end_turn` / `max_tokens` / `tool_use` / `stop_sequence`）、`count_tokens`（未常駐は autoload、未対応ランタイムは 501→HTTP 503 `api_error`） | ✅ |
| 128 tools 上限、OpenAI 経路と同一のキュー／キャンセル／レーン回復 | ✅ 共有 |
| `thinking:{"type":"disabled"}` | ⚠️→**本リリースで修正**（no-op 受理） |
| 拡張思考の有効化 / サーバーサイドツール / `document` / Anthropic側MCP | ⛔ 明示 400（v1 の既知の制約、変更なし） |
| `top_k` | ⚠️ 黙って無視（サンプラーへ渡していない）。低リスク。`mlx-bar.md` の課題へ記録 |
| `cache_control` | 受理して無視。`usage` に `cache_creation_input_tokens` / `cache_read_input_tokens` は出さない（既知） |
| ストリームの `message_delta.usage` に `input_tokens` も載せる | 本家は `output_tokens` のみ。上位互換の付加情報で Claude Code は問題なく解釈（既知・意図的） |

**結論**: Claude Code の実運用（チャット / tool use / count_tokens / 中断・リトライ / 並列 subagent）に
必要な形状は満たしている。拡張思考を Claude Code 側で有効化した場合のみ 400 になる（設計上の既知の制約）。

### 3.2 OpenAI互換 / ZCode

| 項目 | 状態 |
|---|---|
| `max_tokens` / `max_completion_tokens` 省略時のフォールバック（`effective_max_tokens()`、512 は最終手段） | ✅ v1.7.1〜 |
| `stream_options.include_usage` / `include_obfuscation`、末尾の usage 専用チャンク（`choices: []`） | ✅ |
| `tool_choice`（`none` / `auto` / `required` / 特定 function）、128 tools 上限、`delta.role` は最初の1チャンクのみ | ✅ |
| `finish_reason`（`stop` / `length` / `tool_calls` / `content_filter` / `function_call`）。`length` を伝達 | ✅ |
| `/v1/completions`・未知パス・不正メソッドを OpenAI エラー形（`UNSUPPORTED_ENDPOINT` / `HTTP_404` / `HTTP_405`）で返す | ✅ |
| トップレベル `thinking` / `reasoning_effort` / `extra_body.chat_template_kwargs` の正規化と mlx-lm・mlx-vlm 双方への伝播、MLXBar 管理キー（`tools` / `tool_choice` / `tokenize` 等）の上書き拒否 | ✅ v1.3.4〜 |
| SSE キープアライブ（`: mlxbar keep-alive`）、切断時の生成器 close + 孤児レーン回復 | ✅ v1.4.1 / v1.7.0 |
| `response_format`（text 以外）/ `logprobs` / `n>1` | ⛔ 明示エラー（400 / 400 / 422、既知） |
| `usage` のトークン数 | ✅ 通常はランタイム実測（`usage` イベント）。ランタイムが `prompt_tokens` を報告しない稀な経路のみ chars/4 の概算にフォールバック |

**結論**: 重大な非互換は無し。既知の明示エラーは仕様上の制約として `README.md` §API に記載済み。

### 3.3 精査中に見つかったその他（本リリースの対象外）

- **フレークテスト** `Tests/test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats`。
  `heartbeat_interval_seconds: 0.03` が並列フル実行時の CPU 負荷に敏感で、単体実行では安定して成功、
  フル実行では約 1/3 で `"phase": "tool_parse"` ハートビートの出現前に `TOOL_PARSE_FAILED` が来て失敗する。
  互換性・機能とは無関係の観測性テスト。`mlx-bar.md §5` の「テスト」項へ記録。次に `common/server.py` の
  ハートビート周りを触る人が、間隔の大小関係ごと理解したうえで de-flake する。

## 4. 不変条件（守ること）

1. **OpenAI互換 / Anthropic互換の成功経路**（ストリーム / 非ストリーム）の応答ボディ・`usage`・
   SSE イベント列・エラー `code`／`type`・HTTP ステータスは、`thinking:disabled` の1点を除いて不変。
   `thinking:disabled` は「400 → 200 の no-op」で、成功時の出力は変わらない。
2. **`/api/v1/status` は追加のみ**（`loadedModels[].generationTokensPerSecond` /
   `loadedModels[].generatedTokens` / トップレベル `generationTokensPerSecond`）。既存フィールドの
   意味・型は不変。旧 GUI（フィールドを読まない）とも互換。
3. **プール無効時**（`models.pool.enabled=false`）の `status()` は `_legacy.status()` を返すため
   完全に無変更。
4. **`POST /api/v1/models/{id}/unload` と `mlxbarctl` は無変更。** GUI の表示条件のみ拡大。
5. **クラッシュ／メモリ安全性**: `status()` の追加分は fail-safe（欠損で例外を出さない）。
   モデルの evict は従来どおり `_load_lock` 下、`unload_model(force=True)` は当該モデルの生成のみ
   キャンセルし他モデルへ波及しない。
6. **UI 完全性**: 追加文字列は日英とも解決される（`ja` はリテラル、`en` は `Localizable.strings`）。
   数値表示は既存の丸めルールに合わせ、`MenuBarExtra` の再描画を不安定にしない。

## 5. 検証

`TEST_PLAN_v1.9.0.md` を参照。Python は既存 373 件を無改変で維持し、新規 3 件を追加して 376 件。
Swift Debug / Release ビルド、`build-release.sh` + `verify-release.sh`、同梱 Coordinator の
`/api/v1/status` 形と `/anthropic` マウントを確認する。実機（Apple Silicon + 複数モデル常駐）での
モデル別速度表示と個別アンロードの手動確認は `§2` に手順を置き、次に実機を触る人へ引き継ぐ。
