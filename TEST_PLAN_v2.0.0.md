# MLXBar v2.0.0 テスト計画と結果

対象: `/v1/completions`実装、`response_format`（json_object / json_schema）、`n > 1`、
Anthropic Extended Thinking、`/v1/responses`スタブ（以上プレリリース`v2.0.0rc1`から継続）、
および v2.0.0 で追加した `mlxbarctl config set-context-compression`（GUI操作のCLI完全対応の
1件の欠落修正）。詳細は`DESIGN_v2.0.0.md`。

## 1. 自動検証（Python）

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q
```

2026-09-06 結果:

- **406 passed**（`v2.0.0rc1`の403 + CLI新規3）
- 連続3回実行して安定

## 2. 本リリースの新規契約

### 2.1 API拡充（`v2.0.0rc1`から継続）

| # | 契約 | 検証 |
|---|---|---|
| 1 | `/v1/completions`は`prompt`文字列をchat templateなしでそのままWorkerへ渡し、`text_completion`形式で返す | `test_v2_features.py::test_legacy_completions_non_stream_uses_raw_prompt` |
| 2 | `/v1/completions`のstreamは`text_completion.chunk`相当のSSEを返し`[DONE]`で終わる | `test_legacy_completions_stream_emits_text_completion_chunks` |
| 3 | `/v1/completions`は配列プロンプト・`echo`・`suffix`・`logprobs`・`best_of>1`をHTTP 4xxで拒否する | `test_legacy_completions_rejects_array_prompt_and_echo` |
| 4 | `n>1`（非stream）は`choices`をn件返し、`usage.completion_tokens`はn件の合計、`prompt_tokens`は1回分 | `test_chat_completions_n_greater_than_one_returns_multiple_choices`、`test_openai_tools.py::test_multiple_completions_return_n_choices_non_streaming` |
| 5 | `n>1`と`stream=true`の組み合わせはHTTP 400 `UNSUPPORTED_PARAMETER`で拒否される | 同上 |
| 6 | `n`が1〜8の範囲外はHTTP 422で拒否される | `test_chat_completions_rejects_n_out_of_range` |
| 7 | `response_format: json_object`は指示をsystemメッセージへ注入し、有効なJSONはそのまま返す | `test_json_object_mode_accepts_valid_json_and_injects_instruction` |
| 8 | `response_format: json_object`で無効なJSONが生成された場合、黙って返さずHTTP 502 `RESPONSE_FORMAT_INVALID`（`retryable:true`）で拒否する | `test_json_object_mode_rejects_non_json_output` |
| 9 | `response_format: json_schema`はスキーマに一致する出力を受理し、一致しない出力を拒否する | `test_json_schema_mode_validates_against_schema` |
| 10 | `json_schema`で`oneOf`等の未対応キーワードは生成前にHTTP 400で拒否される | `test_json_schema_mode_rejects_unsupported_schema_keywords_up_front` |
| 11 | 未対応の`response_format.type`（`json_object`/`json_schema`/`text`以外）はHTTP 400で拒否、`text`は引き続き無変更で通る | `test_openai_tools.py::test_unsupported_parameters_are_refused_rather_than_silently_ignored` |
| 12 | `logprobs`は引き続きHTTP 400で拒否される（挙動不変） | 同上 |
| 13 | Anthropic `thinking: {type:"enabled", budget_tokens}`は非stream応答の先頭に`thinking` content block（ローカル`signature`付き）を作る | `test_anthropic_extended_thinking_non_stream_emits_thinking_block_with_signature` |
| 14 | 同、streamでは`thinking_delta`ののち`signature_delta`を1回送ってblockを閉じる | `test_anthropic_extended_thinking_streams_thinking_delta_then_signature` |
| 15 | `thinking`未指定時はv1.9.2までと同じ挙動（`reasoning_delta`はcontentへ現れない） | `test_anthropic_thinking_disabled_by_default_keeps_reasoning_internal` |
| 16 | `thinking.budget_tokens >= max_tokens`はHTTP 400で拒否される | `test_anthropic_thinking_budget_must_be_below_max_tokens` |
| 17 | `/v1/responses`はOpenAI形式の`error`で明示的にHTTP 404 `UNSUPPORTED_ENDPOINT`を返す（`{"detail":"Not Found"}`ではない） | `test_responses_api_returns_explicit_unsupported_endpoint`、`test_openai_tools.py::test_responses_api_endpoint_returns_an_openai_shaped_error` |

### 2.2 GUI操作のCLI完全対応（v2.0.0で追加）

| # | 契約 | 検証 |
|---|---|---|
| 18 | `mlxbarctl config set-context-compression`は指定したオプションだけを`contextCompression`へPUTし、`--trigger-percent`は`triggerRatio`（0〜1）へ変換する | `test_cli.py::test_set_context_compression_only_sends_provided_options`、`test_set_context_compression_converts_percent_to_ratio` |
| 19 | 範囲外の値（trigger 50未満/95超、keep-tail 2未満/50超、summary 100未満/4000超）とオプション未指定はサーバーに触れず`ValueError` | `test_cli.py::test_set_context_compression_rejects_out_of_range_and_empty` |

## 3. 回帰

`v2.0.0rc1`の403件はすべて無改変で緑（そのうち3件は`v2.0.0rc1`時点で意図した挙動変更に
合わせて書き換え済み。`n>1`・`response_format: json_object`・`/v1/completions`が正式サポート
になった分）。v2.0.0で追加した3件（CLI）は`cli.py`のみに依存する。

設定スキーマ（`SettingsStore.DEFAULTS`）・Coordinator/Worker間RPC・管理APIハンドラ・
Swift GUI・`Localizable.strings`・既存のtool calling / 画像入力 / コンテキスト自動圧縮の
挙動はすべて無変更。既存`config.json`はそのまま読み込める。

## 4. リリース前の静的デバッグ（外部クライアント互換性）

`openai_compat.py` / `anthropic_compat.py` / `anthropic_stream.py` / `main.py` を読み、
Claude Code / Codex / ZCode / OpenClaw など Anthropic互換・OpenAI互換の各CLIクライアントで
MLXBarが正常動作するかを静的に検証した（`DESIGN_v2.0.0.md §6`）。

- 正確性・クラッシュ安全性・メモリ安全性のバグは**無し**。
- Codex CLIは既定でResponses API（`wire_api = "responses"`）を使う。MLXBarはこれに
  明示的な`404 UNSUPPORTED_ENDPOINT`を返す（フリーズしない）。Codexから使うには
  `~/.codex/config.toml` の `[model_providers.<id>]` で `wire_api = "chat"` を指定する
  必要がある。READMEにCodexの接続手順を追記した。

## 5. 実機確認

`/Applications/MLXBar.app`をv2.0.0へ入れ替え、coordinator再起動後、`Ornith-1.5-35B-A3B-MLX-4bit`
常駐で確認（2026-09-06、公開APIポート`11435`経由。CLIは管理用Unixソケット経由）:

| ケース | 期待 | 結果 |
|---|---|---|
| `/api/v1/health` | バージョン表記の更新確認 | ✅ `{"status":"ok","version":"2.0.0"}`、`mlxbarctl diagnostics` も `2.0.0` |
| `/v1/completions`（`prompt`のみ、chat templateなし） | `text_completion`形式で応答 | ✅ `{"object":"text_completion","choices":[{"text":"Paris. \n\nParis, the capital city"}],"usage":{"prompt_tokens":5,"completion_tokens":8}}` |
| `response_format: json_object` | 有効なJSONを含む応答（前後の空白は許容） | ✅ `content` = `'\n\n{"name": "Alexandra Chen", "age": 34}'`（`json.loads`で解析可） |
| `n: 2`（非stream） | `choices`が2件、`usage.completion_tokens`が合算、`prompt_tokens`は1回分 | ✅ `choices`2件（`index`0,1、両方`'\n\nPONG'`・`finish_reason:stop`）、`completion_tokens=80`、`prompt_tokens=16` |
| Anthropic `thinking: {type:"enabled", budget_tokens:1500}` | `thinking` blockが`signature`付きで先頭、続けて`text` block | ✅ `content[0]`が`type:"thinking"`・`signature`が`mlxbar-local-unsigned:`接頭辞、`content[1]`が正解`136`（17×8）を含む`text`、`stop_reason:end_turn` |
| 新パラメータ無指定の`/v1/chat/completions` | v1.9.2と同一挙動 | ✅ `content='\n\nOK'`（クリーン）、`reasoning_content`分離、`finish_reason:stop` |
| `/v1/responses` | 明示的なUNSUPPORTED_ENDPOINT | ✅ HTTP 404 `{"error":{"code":"UNSUPPORTED_ENDPOINT",...}}`（OpenAI形） |
| `mlxbarctl config set-context-compression --enabled true --trigger-percent 75` | `contextCompression`が`{enabled:true, triggerRatio:0.75}`にマージ、`config get`で確認 | ✅ 応答・`config get`とも `{enabled:true, triggerRatio:0.75, keepTailMessages:8, summaryMaxTokens:800}`。`--enabled false --trigger-percent 70` で既定へ戻せることも確認 |

## 6. ビルド

```sh
./scripts/build-release.sh
VERSION=2.0.0 ./scripts/verify-release.sh
```

Swift Debug/Releaseビルド成功、`codesign --verify --deep --strict`通過、DMG `hdiutil verify`通過を
2026-09-06に確認。DMG SHA-256: `92e8a46c265932db062a551aeae4205ae047a73880ae9d976e9694d01adeca86`。
