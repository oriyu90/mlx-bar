# MLXBar v1.9.2 設計ドキュメント — コンテキスト自動圧縮 + モデル一覧UI刷新

対象: ユーザー提示の設計提案（コンテキスト圧縮・モデル一覧UI刷新）のレビュー・互換性評価を経て
実装した内容。バージョン番号はオーナー指示によりv1.9.2（機能追加だがパッチ番台）。
レビューで見つかった修正点は「4. 設計評価で見つかった修正点」を参照。

## 1. 目的と非目的

**目的:**

1. ZCode / Claude Code / OpenCode のような、会話全体を毎リクエスト送り続けるエージェント型
   クライアントで、長い会話が遅くなる（最終的には`INPUT_TOO_LARGE`で失敗する）問題を、
   任意で有効化できる圧縮機能で緩和する。
2. メニューバーのモデル表示を「主モデル1件を大きく表示＋その下に内訳」から、常駐モデルを
   対等に並べたリストへ刷新し、各モデルの名前コピー・生成中tok/s・状態（ロード中／生成中／
   順番待ち／待機中）を1行で確認できるようにする。

**非目的:**

- クライアントが送った会話ログそのものを書き換える・保存することはしない（MLXBarはステートレス
  なままで、圧縮は毎リクエストその場限り）。
- Coordinator/Worker間のRPCインタフェースやモデルロード・アンロードの挙動は変更しない。
- Anthropic/OpenAIのwire formatは変更しない（圧縮はモデルに渡す直前の内部処理のみ）。

## 2. コンテキスト自動圧縮

### 2.1 実装

新規モジュール [`Coordinator/mlxbar/api/context_compression.py`](Coordinator/mlxbar/api/context_compression.py)
の`maybe_compress_messages()`を、[`openai_compat.py`](Coordinator/mlxbar/api/openai_compat.py)の
`chat()`と[`anthropic_compat.py`](Coordinator/mlxbar/api/anthropic_compat.py)の`_messages()`の
両方から、**モデル解決（`_ensure_requested_model`）とキュー容量チェックの後、実際の生成を始める前**
に1回だけ呼ぶ。

- **トリガー判定**: リクエストで実際に解決されたモデル（`loaded`）の`capabilities.modelMaxTokens`
  から、`WorkerSupervisor.effective_max_prompt_characters()`と同じ式
  （`max(configured, min(modelMaxTokens*4, 10_000_000))`）をCoordinator側で直接計算する
  （`_effective_max_prompt_characters()`）。`messages`と`tools`のJSON文字数がこの値の
  `triggerRatio`（既定0.7）を超えたら発火。
- **圧縮範囲**: 先頭の`system`メッセージと、末尾の直近`keepTailMessages`件（既定8）は必ず逐語
  保持。その間の「中間部分」を圧縮候補とし、`_split_point()`が
  - `tool_calls`を持つassistantメッセージとそれに対応する`tool`メッセージ群を分断しない
  - 画像を含むメッセージを圧縮候補に含めない
  よう境界を左へスナップする。
- **要約生成**: 中間部分の全メッセージ＋固定の英語指示文（"same language as the conversation"
  を明記）を、**すでにロード済みの同じモデル**へ`temperature=0`・`max_tokens=summaryMaxTokens`
  （既定800）で1回投げる。結果を`{"role": "system", "content": "[Earlier conversation summarized]\n" + summary}`
  という1件に置き換える。
- **フォールバック**: 要約生成が例外・空応答・`error`イベントのいずれであっても、元の`messages`を
  そのまま使う（`prompt_cache.py`と同じ「ディスク操作は失敗しても例外を投げない」設計を踏襲）。
- **既定は無効**（`contextCompression.enabled: false`）。理由は4.1節。

### 2.2 設定・可観測性

- `/api/v1/settings`に`contextCompression`セクションを追加（`enabled` / `triggerRatio`
  0.5〜0.95 / `keepTailMessages` 2〜50 / `summaryMaxTokens` 100〜4000、すべて
  `SettingsStore._validate`でバリデーション）。省略時は無効相当の既定値にマージされるため、
  既存の`config.json`は無変更で動く。
- 圧縮が発生すると、`AppState.last_context_compression`（インメモリのみ、DB永続化なし・
  マイグレーション不要）に`{originalChars, compressedChars, droppedMessages, triggerRatio, at}`
  を記録し、`/api/v1/status`の`contextCompression`フィールドとして返す。メニューバーは
  これを`Compacted the conversation (X→Y chars)`として表示する（1.4節のとおり、
  無言で会話内容を書き換えない）。
- Settings画面に新規セクション「コンテキスト自動圧縮」（トグル＋3スライダー＋注意書き）を追加。

## 3. モデル一覧UIの刷新

`Sources/MLXBar/MenuBar/MenuBarView.swift`のヘッダーから「Loaded · engine · name」の大表示を
削除し、常駐モデル（`residentModels`、主モデルを含む）を1つのリストとして表示する。ロード中で
まだプールのスロットになっていない新規ロードは、`loadingModelName`から合成した仮想行として
同じリストの先頭に挿入する（`MenuBarView.displayRows`）。

各行（`modelRow`）は3段構成:

1. 左にコピーボタン（`model.copyModelName(_:)` — 主モデル専用だった`copyLoadedModelName()`を
   汎用化し、どの常駐モデルの名前もコピーできるようにした）＋モデル名＋（該当すれば）
   Quick Chat/アンロード（引数なし）の対象であることを示す「既定」バッジ＋既存の📌／⏏ボタン。
2. 既存の内訳行（エンジン・メモリ・並列数・固定バッジ、そのまま維持）。
3. 新規のステータス行: `ResidentModel.activityState`（`poolState`/`activeLeases`/
   `laneQueueDepth`から導出、バックエンド無変更）が「ロード中…／応答生成中／順番待ち／待機中」
   を判定し、生成中のみ既存の`generationRateText(japanese:)`でtok/sを併記する。

`laneQueueDepth`は`model_pool.py`にすでに存在した値（Swift `ResidentModel`に1フィールド
追加しただけ）。**Coordinator/Workers（Python側）は本節について無変更。**

## 4. 設計評価で見つかった修正点

ユーザー提示の設計提案に対して実装前後で行った互換性レビューの結果。

### 4.1 既定を「無効」に確定

提案1.5節で保留にしていた論点。ZCode/OpenCode/Claude Codeは「モデルが会話全体を実際に見ている」
ことを前提に動作しており、要約は逐語の代替にならない（正確なコード差分の参照など、要約で
劣化しうる場面がある）。**このリリースでは既定を無効に固定し、有効化は明示的なSettings操作の
みとした。** これにより、v1.9.2を適用しただけでは既存のZCode/OpenCode/Claude Code運用に
一切の挙動変化がない（`config.json`にキーが無い状態はデフォルトのマージで`enabled: false`
になる）。

### 4.2 発火閾値をプール全体ではなく「実際に解決されたモデル」基準に修正

実装前レビューで発見: `ModelPoolSupervisor.effective_max_prompt_characters()`は常に
**プールの主スロット**の値を返す（`model_pool.py`の既存実装、`_primary_slot()`経由）。
これをそのまま圧縮のトリガー閾値に使うと、複数モデル常駐時に主モデルでないモデルへの
リクエストで閾値がずれる（本来より早すぎる/遅すぎるタイミングで発火しうる）。

修正: `context_compression.py`に`_effective_max_prompt_characters(loaded, settings)`を追加し、
`WorkerSupervisor.effective_max_prompt_characters()`と同じ式を、**そのリクエストで
`_ensure_requested_model()`が実際に解決した`loaded`の`capabilities.modelMaxTokens`**から
直接計算する。プール層のショートカットに依存しなくなったため、この不整合は起きない。

### 4.3 ツール定義・wire formatへの影響なし（確認事項）

- `tools`/`tool_choice`は`messages`とは別に渡っており、圧縮の対象外（常に完全な形でモデルへ
  渡る）。ツール定義が欠落してエージェントの動作が壊れる経路はない。
- `_split_point()`のtool_calls/toolペア保護により、圧縮後もOpenAI/Anthropic互換の
  メッセージ列として不正な形（対応する`tool`結果を欠いた`tool_calls`など）にはならない。
  Vercel AI SDK等の厳格なパーサ（v1.9.1 Fix 1の対象）に対する回帰はない。
- Anthropicの`cache_control`ヒントは元々「受理するが無視する」実装（`anthropic_compat.py`の
  モジュールdocstринг）なので、圧縮でメッセージ境界が変わっても新たな非互換は生まれない。

### 4.4 キャンセル安全性

要約生成の内部呼び出し（`request_id + "-compact"`）は`except Exception`でのみ捕捉しており、
`asyncio.CancelledError`（`BaseException`のサブクラス）は握りつぶさずそのまま伝播する。
クライアント切断時に圧縮処理が例外を隠して中途半端な状態を作ることはない。

## 5. 影響範囲

| 項目 | 内容 |
|---|---|
| 新規ファイル | `Coordinator/mlxbar/api/context_compression.py`、`Tests/test_context_compression.py` |
| 変更（Python） | `settings.py`（スキーマ追加）、`state.py`（`last_context_compression`）、`api/management.py`（status応答）、`api/openai_compat.py`／`api/anthropic_compat.py`（呼び出し） |
| 変更（Swift） | `MenuBarViewModel.swift`（`ResidentModel.laneQueueDepth`/`activityState`、`copyModelName`、`setContextCompressionSettings`、`lastContextCompression*`）、`MenuBarView.swift`（一覧刷新）、`MLXBarSettingsView.swift`（新規セクション）、`en.lproj/Localizable.strings`（新規キー） |
| 後方互換性 | 設定スキーマは追加のみ（省略時は無効相当のデフォルトにマージ）。Coordinator/Worker間RPC、公開API（OpenAI/Anthropic wire format）、モデルロード／アンロードの挙動は無変更。UI刷新はSwift側のみでバックエンド無変更。 |
| リスク | コンテキスト圧縮は既定無効のため、v1.9.2を適用しただけでは既存運用に挙動変化なし。有効化した場合のみ、圧縮が発生したリクエストで応答が要約に基づく可能性がある（GUI/ログに明示）。 |

## 6. 検証

自動テスト・実機確認は`TEST_PLAN_v1.9.2.md`を参照。
