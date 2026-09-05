# MLXBar v2.0.0rc1 テスト計画と結果

対象: `/v1/completions`実装、`response_format`（json_object / json_schema）、`n > 1`、
Anthropic Extended Thinking、`/v1/responses`スタブ。詳細は`DESIGN_v2.0.0rc1.md`。

## 1. 自動検証（Python）

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q
```

2026-09-05 結果:

- **403 passed**（v1.9.2までの388 − 3（挙動変更に伴う書き換え）+ 3（書き換え後）+ 15（新規）＝403）
- 連続3回実行して安定

## 2. 本リリースの新規契約

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

## 3. 回帰

既存403件中388件相当（v1.9.2までの全契約）は無改変で緑。以下3件は**意図した挙動変更**に伴い
アサーションを書き換えた（削除・弱体化ではなく、新しい正しい挙動への更新）:

- `test_multiple_completions_are_rejected_cleanly` →
  `test_multiple_completions_return_n_choices_non_streaming`（`n>1`は今回から正式サポート）
- `test_structured_output_is_refused_rather_than_silently_ignored` →
  `test_unsupported_parameters_are_refused_rather_than_silently_ignored`（`json_object`は
  今回から正式サポート。未知のtypeと`logprobs`の拒否は引き続き検証）
- `test_legacy_completions_endpoint_returns_an_openai_shaped_error` →
  `test_responses_api_endpoint_returns_an_openai_shaped_error`（`/v1/completions`は今回から
  実装済み。「明示的なUNSUPPORTED_ENDPOINT」という契約自体は`/v1/responses`が引き継ぐ）

設定スキーマ・Coordinator/Worker間RPC・既存のtool calling / 画像入力 / コンテキスト自動圧縮の
挙動はすべて無変更（変更ファイルに設定マイグレーションが一切含まれないため、既存`config.json`
はそのまま読み込める）。

## 4. 実機確認

`/Applications/MLXBar.app`をv2.0.0rc1へ入れ替え、coordinator再起動後、`Ornith-1.5-35B-A3B-MLX-4bit`
常駐で確認（2026-09-05、公開APIポート`11435`経由）:

| ケース | 期待 | 結果 |
|---|---|---|
| `/v1/completions`（`prompt`のみ、chat templateなし） | `text_completion`形式で応答 | ✅ `{"object":"text_completion","choices":[{"text":"a city located on the Seine River.","finish_reason":"length"}]}` |
| `response_format: json_object` | 有効なJSONを含む応答（前後の空白は許容） | ✅ `content` = `"\n\n{\"name\": \"Elena Marchetti\", \"age\": 34}"`（`json.loads`で解析可、200 OK） |
| `n: 2`（非stream） | `choices`が2件、`usage.completion_tokens`が合算 | ✅ `choices`2件（`index`0,1）、`completion_tokens`合計20（各10） |
| Anthropic `thinking: {type:"enabled", budget_tokens:40}` | `thinking` blockが`signature`付きで先頭に、続けて`text` block | ✅ `content[0]`が`type:"thinking"`・`signature`が`mlxbar-local-unsigned:`接頭辞、`content[1]`が正しい計算結果`132`を含む`text` |
| 新パラメータ無指定の`/v1/chat/completions` | v1.9.2と同一挙動 | ✅ `content`/`reasoning_content`分離は従来どおり |
| `/v1/responses` | 明示的なUNSUPPORTED_ENDPOINT | ✅ `{"error":{"code":"UNSUPPORTED_ENDPOINT",...}}` |
| `/health` | バージョン表記の更新漏れ確認 | ✅ `{"status":"ok"}`（`/api/v1/health`は`{"status":"ok","version":"2.0.0rc1"}`） |

## 5. ビルド

```sh
./scripts/build-release.sh
VERSION=2.0.0rc1 ./scripts/verify-release.sh
```

Swift Debug/Releaseビルド成功、`codesign --verify --deep --strict`通過、DMG `hdiutil verify`通過を
2026-09-05に確認。DMG SHA-256: `4ac1329b11eaf9a68a118998e9064f69e115f2c9ec7479170fbb095d21ed08e9`。
