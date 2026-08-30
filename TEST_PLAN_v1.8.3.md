# MLXBar v1.8.3 テスト計画と結果

対象: Ornith 1.5系（mlx-lmエンジン）がtool呼び出しでクラッシュする不具合の修正
（`Workers/mlx_lm_worker/adapter.py` + `Workers/common/tool_calls.py::normalize_tool_call_messages`）。

v1.8.2以前の全契約は回帰として維持する。管理API・Anthropic互換API・`mlx_vlm_worker`は無変更。
設定schema version 1は不変。

## 1. 自動検証

```sh
cd Coordinator && uv run pytest ../Tests -q
```

2026-08-31 結果:

- Python: **367 passed**（v1.8.2の361 + v1.8.3の6）

## 2. 本不具合の新規契約

| 契約 | 検証 |
|---|---|
| `arguments`が文字列ならJSONとしてパースしdictへ変換する | `test_worker_server::test_normalize_tool_call_messages_parses_string_arguments` |
| パース失敗時は空dict`{}`にフォールバックする（例外を投げない） | `test_worker_server::test_normalize_tool_call_messages_falls_back_to_empty_dict_on_bad_json` |
| 既にdictの`arguments`はそのまま（変換不要なら同一オブジェクトを返す） | `test_worker_server::test_normalize_tool_call_messages_passes_through_dict_arguments_unchanged` |
| tool_callsを含まない会話は完全no-op（同一オブジェクトを返す、文字列以外の入力も安全） | `test_worker_server::test_normalize_tool_call_messages_is_a_noop_without_tool_calls` |
| `MLXLMAdapter.stream()`が、`\|items`でarguments を反復する偽テンプレートに対し、tool呼び出し2ターン目でもクラッシュしない | `test_worker_server::test_mlx_lm_parses_tool_call_arguments_back_into_a_mapping` |
| tool_callsを含まない会話では、テンプレートへ渡される`prompt`が呼び出し元と同一オブジェクト | `test_worker_server::test_mlx_lm_leaves_tool_call_free_messages_untouched` |

### 修正前は失敗することの確認

`test_mlx_lm_parses_tool_call_arguments_back_into_a_mapping`は、修正前の
`mlx_lm_worker/adapter.py`（`normalize_tool_call_messages`呼び出しを外したバージョン）に対しては
`TypeError: Can only get item pairs from a mapping.`で失敗することを、一時的なロールバックで確認済み。

### 回帰（既存契約が壊れていないことの確認）

| 契約 | 検証 |
|---|---|
| `mlx_lm`のchat_template_kwargs保持・tool_choice非対応時のフォールバック（v1.7.1由来） | `test_worker_server::test_mlx_lm_chat_template_kwargs_are_preserved_with_tools` PASS |
| `mlx_vlm`側のtool_choice非対応フォールバック | `test_worker_server::test_mlx_vlm_retries_without_tool_choice_when_template_rejects_it` PASS |
| モデルプール・CLI・Anthropic互換（v1.8.1/v1.8.2機能）は不変 | 該当テスト全PASS（361件） |

## 3. 実機確認（2026-08-31、Apple Silicon実機・実モデルで実施）

開発用チェックアウトから、本番と同一の`common/server.py`のHTTP経路でCoordinator＋Workerを
別ポート（11439）で起動し、実際にクラッシュしていたリクエストをそのまま再生した。

- `Ornith-1.5-9B-MLX-8bit`: 1ターン目→2ターン目（tool呼び出し）を5回連続実行、
  全て`GENERATION_FAILED`なしで正常応答。
- `Ornith-1.5-9B-MLX-8bit`: 並列2件のtool呼び出し（`web_search`+`web_fetch`）を含む会話でも
  `GENERATION_FAILED`は発生しない（モデル自身の出力品質に起因する別種のエラー
  `TOOL_PARSE_FAILED`が時々出ることはあるが、これは本修正と無関係な既知の挙動）。
- `Ornith-1.5-35B-A3B-MLX-4bit`: 同一のtool呼び出しリクエストで正常応答を確認。

## 4. 配布検証（リリース前）

- `sh scripts/build-release.sh` → `sh scripts/verify-release.sh`。同梱Coordinatorが`version 1.8.3`。
- `CFBundleShortVersionString = 1.8.3` / `CFBundleVersion = 31`。
- `/Applications/MLXBar.app`を本ビルドへ入れ替え、実際のAPIエンドポイント経由で
  上記§3のtool呼び出しシナリオを再確認する。
- Apple公証: 未実施（ad-hoc署名。`mlx-bar.md`参照）。

## 5. 既知の未解決事項（本リリースの対象外）

- Ornith 1.5系は時折、tool呼び出しの出力形式が崩れて`TOOL_PARSE_FAILED`になることがある
  （本修正の対象である`GENERATION_FAILED`とは別のエラーコード）。これはモデル自身の
  出力品質の問題であり、mlx-bar側のバグではないと考えられる。
