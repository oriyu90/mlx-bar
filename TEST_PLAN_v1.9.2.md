# MLXBar v1.9.2 テスト計画と結果

対象: コンテキスト自動圧縮（既定無効）、メニューバーのモデル一覧UI刷新。
v1.9.1以前の全契約は回帰として維持する。Coordinator/Workers間のRPC・OpenAI/Anthropic
互換APIのwire format・設定schema version 1は不変。

## 1. 自動検証（Python）

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q
```

2026-09-05 結果:

- Python: **388 passed**（v1.9.1の380 + v1.9.2の8）
- 連続3回実行して安定（`test_buffered_tool_generation_still_emits_heartbeats`はv1.9.1と同じ
  既知のフル並列時フレークで据え置き、単体では常に緑）

## 2. 本リリースの新規契約

| # | 契約 | 検証 |
|---|---|---|
| 1 | `contextCompression.enabled=false`（既定）では、`messages`はモデルへ渡す直前まで一切変更されず、要約用の追加生成も発生しない | `test_context_compression::test_disabled_by_default_leaves_messages_untouched` |
| 2 | 有効時、閾値を超えた会話でシステムプロンプトと直近`keepTailMessages`件は逐語のまま送られ、中間部分は1件の要約`system`メッセージに置き換わる。かつ`tool_calls`/`tool`のペアは分断されない（要約後の全メッセージを走査し、`tool_calls`を持つassistantの直後が必ず対応する`tool`であることを確認） | `test_context_compression::test_triggers_and_preserves_tool_call_pairing` |
| 3 | 要約用の生成が失敗（`error`イベント）した場合、実際の生成には元の`messages`がそのまま渡り、`last_context_compression`は`None`のまま | `test_context_compression::test_falls_back_to_original_messages_when_summarization_fails` |
| 4 | 画像を含むメッセージは圧縮候補の境界計算で必ずガード側（tail側）に残る | `test_context_compression::test_split_point_keeps_image_turns_out_of_the_compressible_middle` |
| 5 | 会話がguard tail以下の長さなら圧縮候補なし（`None`） | `test_context_compression::test_split_point_returns_none_when_conversation_is_shorter_than_the_tail` |
| 6 | `contextCompression`の設定値は未設定時に既定値（`enabled:false, triggerRatio:0.7, keepTailMessages:8, summaryMaxTokens:800`）にマージされ、範囲外の値は`SettingsStore.update()`が`ValueError`で拒否する | `test_context_compression::test_settings_default_to_disabled` / `test_settings_reject_out_of_range_trigger_ratio` / `test_settings_accept_a_valid_patch` |

モデル一覧UI（Swift）はpytestの対象外。3節の実機確認で検証する。

## 3. 回帰（v1.9.1以前の契約が不変であること）

既存テストスイート376件相当がすべて無改変で緑（tool call streaming、非ストリーム推論、
Anthropic互換、ワーカーのtools有無経路、モデルプール、prompt cache、hardening系）。
`context_compression.py`はOpenAI/Anthropic双方の`chat()`/`_messages()`ハンドラで
「モデル解決後・生成開始前」の1箇所からのみ呼ばれ、`contextCompression.enabled=false`の
早期returnにより既存経路への影響はコード上ゼロ（`if not config.get("enabled", False): return messages, None`
が最初の分岐）。

## 4. 実機確認

`/Applications/MLXBar.app`をv1.9.2へ入れ替え、coordinator再起動後、`Ornith-1.5-35B-A3B-MLX-4bit`
常駐で確認（2026-09-05）:

| ケース | 期待 | 結果 |
|---|---|---|
| `contextCompression`無効（既定）・`tools`なし・非stream | v1.9.1と同一挙動（`content`クリーン、`reasoning_content`分離） | ✅ `content='\n\nPONG'`、`reasoning_content`に思考、本文にthinkタグ無し |
| `contextCompression`有効・26件703,300文字の合成会話（tool呼び出しなし） | 圧縮が発火し`/api/v1/status`の`contextCompression`に縮小結果が載る | ✅ `{originalChars:703300, compressedChars:88963, droppedMessages:21, triggerRatio:0.5}` |
| `contextCompression`有効・中間部分単独でも`effectiveMaxPromptCharacters`超過（約1.7MB） | 要約生成が`INPUT_TOO_LARGE`で失敗→無圧縮の元プロンプトへフォールバック（想定済み縮退、`DESIGN_v1.9.2.md §2.1`末尾） | ✅ ログに`Context compression summary failed; using the uncompressed prompt: INPUT_TOO_LARGE`、フォールバック後の実生成も同じ理由でエラー（設計どおり） |
| `/api/v1/health` / `/api/v1/status` | バージョン表記の更新漏れ確認 | ✅ `{"status":"ok","version":"1.9.2"}` |

検証後、`contextCompression`は出荷時既定（`enabled:false, triggerRatio:0.7, keepTailMessages:8,
summaryMaxTokens:800`）へ戻した。詳細な経緯・環境固有の注意点（`generation.maxTokens`が
このMacでは262,131に設定済み）は`common-rules-document`の`mlx-bar.md §0-Q`を参照。

## 5. ビルド

```sh
./scripts/build-release.sh
VERSION=1.9.2 ./scripts/verify-release.sh
```

Swift Debug/Releaseビルド成功、`codesign --verify --deep --strict`通過、DMG `hdiutil verify`通過を
2026-09-05に確認。DMG SHA-256: `b2fcd1482e0b73e227aac577b95d9b32fb2b10ec7ff5cb24df70ff7f6076bcf5`。

（実機実行後に追記）
