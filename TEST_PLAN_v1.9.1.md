# MLXBar v1.9.1 テスト計画と結果

対象: 2026-09-02 コード監査4件の修正
（`Coordinator/mlxbar/api/openai_compat.py` の `_tool_call_stream_chunks` /
非ストリーム `chat()` / `_merge_tool_call_deltas`、`Coordinator/mlxbar/api/anthropic_stream.py`
の `_merge_tool_call_delta`、`Workers/common/server.py` の括弧追加）。

v1.9.0以前の全契約は回帰として維持する。Coordinator のルート・管理API・設定schema version 1、
Swift GUI は不変。

## 1. 自動検証

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q
```

2026-09-02 結果:

- Python: **380 passed**（v1.9.0の376 + v1.9.1の4）
- 連続3回実行して安定（フレーク `test_buffered_tool_generation_still_emits_heartbeats` は
  直列実行では常に緑。フル並列時のみ約1/3で失敗する既知の観測性フレークで据え置き）

## 2. 本監査4件の新規契約

| # | 契約 | 検証 |
|---|---|---|
| 1 | `tool_calls` イベントに call が2件入るストリームでも、`tool_calls` を含むチャンク全体で `delta.role="assistant"` は1回だけ。両 call の先頭チャンク・引数チャンクは従来どおり流れる | `test_openai_tools::test_streaming_two_tool_calls_still_send_role_only_once` |
| 1 | 単一 tool call のストリーム（`tool_call_delta` 経路）で `role` は1回（既存契約の維持） | `test_openai_tools::test_streaming_tool_call_deltas_send_role_only_on_the_first_chunk`（既存・無改変） |
| 2 | `tools` 無し・非ストリームで `reasoning_delta` → `delta` の順に届くと、`message.content` は本文のみ、`message.reasoning_content` に推論が入る（`content` に混ざらない） | `test_openai_tools::test_non_streaming_reasoning_is_returned_not_dropped` |
| 2 | `tools` 無し・ストリームでは従来どおり `delta.reasoning_content` で分離（既存契約の維持） | `test_openai_tools::test_reasoning_delta_uses_openai_compatible_reasoning_content_field`（既存・無改変） |
| 3 | `_merge_tool_call_deltas` に `{"index": None, …}` を渡してもクラッシュせず、位置0の call として畳まれる | `test_openai_tools::test_merge_tool_call_deltas_tolerates_a_null_index` |
| 3 | `AnthropicMessageBuilder.handle` に `index: None` の `tool_call_delta` を渡してもクラッシュせず、`tool_use` ブロックが1件生成される | `test_anthropic_compat::test_tool_call_delta_with_a_null_index_does_not_crash_the_builder` |
| 4 | （挙動不変のためテスト追加なし。既存のロード失敗分類テストで担保） | `test_hardening.py` / `test_worker_server.py` 一式（無改変で緑） |

### 修正前は失敗することの確認

`Coordinator/mlxbar/api/` と `Workers/common/server.py` を v1.9.0 へ戻し、新規テストのみ適用して実行:

```
FAILED test_streaming_two_tool_calls_still_send_role_only_once          （role が 2 回）
FAILED test_non_streaming_reasoning_is_returned_not_dropped             （reasoning_content キーが無い）
FAILED test_merge_tool_call_deltas_tolerates_a_null_index               （TypeError: int() argument …）
FAILED test_tool_call_delta_with_a_null_index_does_not_crash_the_builder（同上）
```

修正適用後は4件すべて PASS、全体 380 件 PASS。

## 3. 回帰（v1.9.0以前の契約が不変であること）

| 契約 | 検証 |
|---|---|
| OpenAI 互換ストリームの基本形（初期 `role` チャンク、`created` 固定、終端1回、`[DONE]`） | `test_openai_tools::test_stream_starts_with_role_and_translates_internal_heartbeat_to_sse_comment` ほか |
| 単一 tool call のストリーム（`tool_calls` イベント経路）が `delta` と終端 `finish_reason` を出す | `test_openai_tools::test_streaming_tool_calls_use_delta_and_terminal_finish_reason` |
| 非ストリームの tool 応答・メッセージ履歴が OpenAI 互換 | `test_openai_tools::test_non_streaming_tool_response_and_message_history_are_openai_compatible` |
| 非ストリームの通常応答 `content` | `test_openai_tools::test_*`（`"content": "ok"` 系、無改変） |
| Anthropic 互換ストリームの tool_use（`input_json_delta` / `stop_reason=tool_use`） | `test_anthropic_compat::test_streaming_tool_use_uses_input_json_delta_and_tool_use_stop_reason` |
| Anthropic 非ストリームの形・usage・stop_reason マッピング一式 | `test_anthropic_compat.py` 一式（無改変） |
| ワーカーの `tools` あり／なし経路（推論分離・tool マークアップ・打ち切り・stop シーケンス・ハートビート） | `test_worker_server.py` 一式（無改変） |
| ロード失敗・生成失敗の日本語分類（#4 の対象コード） | `test_hardening.py` / `test_worker_server.py`（無改変で緑） |
| モデルプール・レプリカ・自動ロード・prompt cache | `test_model_pool.py` / `test_model_replicas.py` / `test_prompt_cache_reuse.py`（無改変） |

## 4. 実機確認

稼働中の `/Applications/MLXBar.app` を v1.9.1 へ入れ替え、coordinator 再起動後、
LAN の OpenAI 互換エンドポイント（`http://192.168.0.165:11435/v1`、OpenClaw が使う経路と同一）で:

| ケース | 期待 | 結果 |
|---|---|---|
| 推論モデル・`tools` **なし**・**非stream** | `content` は本文のみ、`message.reasoning_content` に思考、`<think>`/`</think>` は本文に無し | （実機実行後に追記） |
| 推論モデル・`tools` **なし**・stream | 従来どおり `delta.reasoning_content` で分離（回帰なし） | （実機実行後に追記） |
| 通常モデル・`tools` あり・stream（tool call 1件） | v1.9.0 と同一（`role` 1回、引数チャンク、`finish_reason=tool_calls`） | （実機実行後に追記） |
| `tools` あり ＋ 先行 `tool` ロールメッセージ（v1.8.3 のクラッシュ事例の回帰ガード） | `GENERATION_FAILED` なし | （実機実行後に追記） |
| `/api/v1/health` が `1.9.1` を返す | バージョン表記の更新漏れ確認 | （実機実行後に追記） |

## 5. ビルド

```sh
./scripts/build-release.sh
VERSION=1.9.1 ./scripts/verify-release.sh
```

Swift Debug / Release ビルド成功、`codesign --verify --deep --strict` 通過、
DMG `hdiutil verify` 通過を確認する。
