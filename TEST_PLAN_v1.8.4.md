# MLXBar v1.8.4 テスト計画と結果

対象: 推論対応モデルへ `tools` を含まないリクエストを送ると `<think>…</think>` の中身が
`content` へ混ざる不具合の修正
（`Workers/common/server.py` + `Workers/common/tool_calls.py::IncrementalToolStream`
+ `Workers/mlx_lm_worker/adapter.py` / `Workers/mlx_vlm_worker/adapter.py`）。

v1.8.3以前の全契約は回帰として維持する。Coordinator（OpenAI互換 / Anthropic互換、
stream / non-stream）・管理API・設定schema version 1は不変。

## 1. 自動検証

```sh
cd Coordinator && uv run pytest ../Tests -q
```

2026-08-31 結果:

- Python: **373 passed**（v1.8.3の367 + v1.8.4の6）

## 2. 本不具合の新規契約

| 契約 | 検証 |
|---|---|
| `tools` 無し・テンプレートが先頭 `<think>` を開くモデルで、推論文は `reasoning_delta`、本文は `delta` に分離され、`<think>`/`</think>` はどのイベントにも出ない | `test_worker_server::test_preopened_reasoning_is_separated_without_tools` |
| `tools` 無し・ストリーム途中で分割された `<think>` タグでも正しく分離する | `test_worker_server::test_inline_split_think_tags_are_separated_without_tools` |
| `tools` 無し・非推論モデル（`<think>` を一切出さない）は従来どおり同じ `delta` 列でストリームされ、`reasoning_delta` は出ない | `test_worker_server::test_non_reasoning_model_without_tools_streams_unchanged` |
| `tools` 無しで自発的に `<tool_call>` マークアップを吐くモデルは、それを本文として可視のまま流し、`tool_calls` / `error`（`TOOL_PARSE_FAILED`）を出さない | `test_worker_server::test_spontaneous_tool_markup_without_tools_stays_visible` |
| `tools` 無し・`</think>` が来ないまま打ち切られた推論は `reasoning_delta` に入り、`delta`（本文）へは1文字も漏れない | `test_worker_server::test_truncated_reasoning_without_tools_does_not_leak_into_content` |
| `IncrementalToolStream(reasoning_only=True)` は `<tool_call>` を検出せず（`tool_detected` は False のまま）本文として通し、`<think>`/`</think>` の切り分けは行う | `test_worker_server::test_incremental_tool_stream_reasoning_only_ignores_tool_markup` |

### 修正前は失敗することの確認

修正前のソース（`Workers/` のみ元に戻し、新規テストは適用）で対象6件を実行:

```
FAILED test_preopened_reasoning_is_separated_without_tools          （推論文が delta に混入）
FAILED test_inline_split_think_tags_are_separated_without_tools     （同上）
FAILED test_truncated_reasoning_without_tools_does_not_leak_into_content
FAILED test_incremental_tool_stream_reasoning_only_ignores_tool_markup  （reasoning_only 引数が無い）
PASSED test_non_reasoning_model_without_tools_streams_unchanged     （回帰ガード。前後どちらでも緑）
PASSED test_spontaneous_tool_markup_without_tools_stays_visible     （回帰ガード。前後どちらでも緑）
```

修正適用後は6件すべてPASS、全体373件PASS。

## 3. 回帰（v1.8.3以前の契約が不変であること）

| 契約 | 検証 |
|---|---|
| `tools` あり経路の推論 / tool マークアップ分離は不変 | `test_worker_server::test_reasoning_and_tool_markup_are_separated_while_streaming` |
| `tools` あり・通常応答の逐次ストリームは不変 | `test_worker_server::test_normal_tool_capable_response_is_streamed_incrementally` |
| `tools` あり・解析不能な tool call は `TOOL_PARSE_FAILED` を返す | `test_worker_server::test_detected_but_unparseable_tool_call_returns_explicit_error` |
| ランタイム各方言の tool マーカーは可視出力から除外される（デフォルト構築） | `test_worker_server::test_every_runtime_tool_marker_is_withheld_from_visible_output` |
| `reasoning_delta` は OpenAI 互換の `reasoning_content` フィールドで配信される | `test_openai_tools::test_reasoning_delta_uses_openai_compatible_reasoning_content_field` |
| tool call が無い出力でも `<think>` は中身ごと除去される（`parse_tool_markup` 単体） | `test_openai_tools::test_reasoning_markup_is_stripped_even_without_a_tool_call` |
| Anthropic 互換のストリーム契約（`reasoning_delta` 非転送を含む） | `test_anthropic_compat.py` 一式 |

## 4. 実機確認

稼働中の `/Applications/MLXBar.app`（v1.8.4 をビルド・再インストール後）へ、
LAN の OpenAI 互換エンドポイント（`http://192.168.0.165:11435/v1`、OpenClaw が使う経路と同一）で:

2026-08-31 結果（`/Applications/MLXBar.app` を v1.8.4 へ入れ替え、coordinator 再起動後）:

| ケース | 期待 | 結果 |
|---|---|---|
| `Ornith-1.5-35B-A3B-MLX-4bit`・`tools` **なし**・非stream | `content` に思考文・`</think>` が出ない | ✅ `content='\n\nPONG'`（修正前は `'The user is asking me to…\n</think>\n\nPONG'`） |
| 同上・stream | `content` はクリーン、思考は `reasoning_content` デルタ | ✅ `content='\n\n9'`、`reasoning_content` に CoT |
| `Ornith-1.5-35B-A3B-MLX-4bit`・`tools` **あり**・stream | v1.8.3 と同じ（回帰なし） | ✅ `content='\n\nPONG'`、`reasoning_content` に CoT |
| `tools` **あり** ＋ 先行する `tool` ロールメッセージ（v1.8.3 のクラッシュ事例） | `GENERATION_FAILED` なし | ✅ 正常応答、`finish=stop` |

非推論モデル・`tools` なしの無変更ストリームは
`test_non_reasoning_model_without_tools_streams_unchanged`（自動）で担保。

## 5. ビルド

```sh
./scripts/build-release.sh
VERSION=1.8.4 ./scripts/verify-release.sh
```

Swift リリースビルド成功 / `codesign --verify --deep --strict` 通過 / DMG `hdiutil verify` 通過を確認する。
