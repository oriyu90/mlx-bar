# MLXBar v1.7.1 テスト計画と結果

対象:
1. メニューバーに全常駐モデルを一覧表示（Issue 1、GUI表示層のみ）
2. エラーメッセージの日本語化（Issue 2、`code`→表示言語の解決＋サーバ側`message`保証）
3. OpenAI互換クライアント（Zed / Cline / OpenCode）対応（Issue 3）

v1.7.0 の全契約は回帰として維持する。`models.pool` の不変条件（`generationConcurrency=1`
＝v1.6.2等価、`enabled=false`＝v1.6.1バイト等価）に変更なし。

## 1. 自動検証

```sh
cd Coordinator && uv run pytest ../Tests -q
swift build --disable-sandbox -c debug
swift build --disable-sandbox -c release
```

2026-08-29 結果:

- Python: **316 passed**（v1.7.0 の 307 + v1.7.1 の 9）
- Swift Debug: **成功**
- Swift Release: **成功**（`swift build -c release`。DMG パッケージングと署名はリリース前に実施）

v1.7.1 の新規契約:

| 契約 | 検証 |
|---|---|
| `max_tokens` / `max_completion_tokens` 省略時は有効上限を既定 | `test_omitted_max_tokens_falls_back_to_the_configured_ceiling` |
| 明示 `max_tokens` は従来どおり尊重 | `test_explicit_max_tokens_is_still_honoured` |
| 有効上限を返せないworkerでは512にフォールバック | `test_max_tokens_without_a_worker_ceiling_still_defaults_to_512` |
| `/v1/completions` は OpenAI 形式の `UNSUPPORTED_ENDPOINT` | `test_legacy_completions_endpoint_returns_an_openai_shaped_error` |
| 未知パスも `{"error": {...}}` 形式 | `test_unknown_path_uses_the_openai_error_shape_not_starlette_default` |
| OpenAI エラー応答は必ず非空 `message` を持つ | `test_every_openai_error_response_carries_a_message` |
| ストリーミング tool call の `delta.role` は最初の1チャンクのみ | `test_streaming_tool_call_deltas_send_role_only_on_the_first_chunk` |
| 常駐1件・未知モデル名 → その常駐へ振り分け | `test_a_made_up_model_name_routes_to_the_sole_resident_instead_of_erroring` |
| 管理APIエラーは `code` だけでなく日本語 `message` を返す | `test_management_errors_carry_a_japanese_message_not_a_bare_code` |

回帰で特に確認したもの:

- `test_an_unknown_model_is_missing_rather_than_busy_while_another_request_runs`
  （`make_autoload_client` は `loaded_models` を持たないため単一常駐フォールバックの対象外。
  複数常駐 `_TwoModelPool` も `loaded_models()` が2件のため対象外。）
- `test_chat_completions_route_to_the_requested_resident_model` /
  `test_two_resident_models_stream_without_api_layer_serialisation`（複数常駐ルーティング不変）
- `test_streaming_tool_calls_use_delta_and_terminal_finish_reason`（`_tool_call_stream_chunks`
  経路の role は元から最初の1回のみ）

## 2. 手動GUI検証（要 Apple Silicon 実機・要モデル本体）

**このビルド環境には MLX ランタイムもモデル本体も無いため未実施。リリース前に現場で通すこと。**

1. 「設定…」→「一般」で言語を「日本語」にする。
2. `models.pool.profiles` に小型モデルを `keepLoaded: true` で登録してサービス再起動 →
   プリロードされることを確認。別の小型モデルを「モデル」画面から手動ロード。
3. メニューバーのポップオーバーに **両方** のモデルが行として表示され、各行にエンジン・
   メモリ予約量（GB）・ピン状態・アンロードボタンがあることを確認。
4. 片方の行のアンロードボタン → そのモデルだけ消え、もう片方は残る。
5. 生成中のモデルの行のアンロード → 日本語で `ENGINE_BUSY` 相当の文言（「別の生成が実行中〜」）。
6. ピン（pin）トグル → `models.pool.profiles` の `keepLoaded` が切り替わる。
7. 「すべてアンロード」→ 全解放（従来どおり）。単一常駐に戻したとき、ヘッダーのみの
   従来表示に戻ることを確認。

## 3. 手動エラー日本語化検証

1. 誤ったAPIキーで `curl -H "Authorization: Bearer wrong" .../v1/models` →
   GUI・クライアントともに日本語の認証エラー。
2. 存在しないIDで `POST /api/v1/models/<id>/load` → 日本語の `MODEL_NOT_FOUND` 文言。
3. 破損した重みフォルダをロード → 「モデルファイルを読み込めませんでした〜」等の日本語。
   `~/Library/Application Support/MLXBar/logs/coordinator.log` に英語原文（`detail`）が残る。
4. 生成中に別モデルへ切替 → 日本語の `ENGINE_BUSY` 文言。
5. 英語UI（言語=English）に切り替えて同じ操作 → 英語文言になること（`ErrorText` の en 側）。

## 4. 手動 OpenAI 互換クライアント検証

Zed（または Cline / OpenCode）で Base URL `http://127.0.0.1:11435/v1`、APIキー、モデルIDを設定。

1. `max_tokens` を送らない通常のチャット → 応答が512で切れず、有効上限まで続く
   （`/api/v1/logs` または「設定…」→「詳細」→「最近のAPIログ」の `max_tokens` 列で確認）。
2. `POST /v1/completions` → `{"error":{"code":"UNSUPPORTED_ENDPOINT", ...}}`。
3. `GET /v1/nonexistent` → OpenAI形式の `error`（`code: HTTP_404`）。
4. tool を使うエージェント操作 → ストリーミングで最初のチャンクだけ `delta.role: "assistant"`。
5. 小型モデルを1つだけ常駐させ、クライアントのモデルIDを実際と違う名前にしても生成できる。
   2つ常駐させると、一致しない名前は従来どおり 404 / 自動ロード判定になる。

## 5. 配布検証（リリース前）

- `scripts/build-release.sh` → `scripts/verify-release.sh`。パッケージされた Coordinator が
  `version 1.7.1` を報告することを確認。
- `CFBundleShortVersionString = 1.7.1` / `CFBundleVersion = 27`。
- Apple 公証: 未実施（ad-hoc 署名。`mlx-bar.md §5` 参照）。
