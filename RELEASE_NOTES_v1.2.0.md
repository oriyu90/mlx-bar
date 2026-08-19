# MLXBar v1.2.0

MLXBar v1.2.0 is a bug-fix release addressing two issues reported by users. Upgrading is recommended for everyone, especially anyone using a reasoning ("thinking") model such as Laguna S 2.1.

MLXBar v1.2.0は、利用者から報告された2件の不具合を修正したリリースです。特にLaguna S 2.1のような推論（thinking）対応モデルを利用している方には更新を推奨します。

## Fixes

- **`<think>` reasoning blocks no longer leak into plain chat replies.** Reasoning-capable models such as Laguna S 2.1 emit a `<think>...</think>` block before their answer. When a response contained no tool call, the shared tool-markup parser (`Workers/common/tool_calls.py`, used by both the mlx-lm and mlx-vlm workers) returned the raw text unmodified, and even the tag-stripping regex on the tool-call path only removed the `<think>` tags themselves, not the reasoning text between them. Both paths are fixed: the parser now strips the whole `<think>...</think>` block, with its content, whether or not a tool call was found.
- **The menu bar popup can now be scrolled when its content is taller than the screen.** It was a fixed-size panel with no scroll view, so status lines, warnings, and controls beyond the screen edge were simply invisible with no way to reach them. It's now wrapped in a scroll view, is a bit wider, and the loaded/loading model line wraps to two lines instead of being cut off mid-name.
- Quick Chat's header could crowd the model name against the temperature slider and max-token stepper at narrow window widths. The model name now sits on its own row.

## 修正

- **`<think>`推論ブロックが通常のチャット応答にそのまま混ざる問題を修正しました。** Laguna S 2.1のような推論対応モデルは、回答の前に`<think>...</think>`ブロックを出力します。ツール呼び出しを伴わない応答では、mlx-lm・mlx-vlm両ワーカーが共有する解析処理（`Workers/common/tool_calls.py`）がテキストをそのまま返しており、さらにツール呼び出しがあった場合のタグ除去用の正規表現も、`<think>`タグ自体は消せてもタグの間にある推論文までは消せていませんでした。両方の経路を修正し、ツール呼び出しの有無にかかわらず`<think>...</think>`ブロックを内容ごと除去するようにしました
- **メニューバーのポップアップが画面の高さを超えたときに、スクロールできるようになりました。** 従来は固定サイズでスクロール手段がなく、画面からはみ出した状態表示・警告・操作ボタンには一切手が届きませんでした。ポップアップをスクロール可能にし、幅も少し広げ、ロード中／ロード済みのモデル名表示は途中で切れず2行まで折り返すようにしました
- クイックチャット画面で、狭い幅のときにモデル名表示が温度スライダーや最大トークン数の調整コントロールと幅を取り合っていた問題を修正しました。モデル名を単独の行に分離しました

## Verification / 検証結果

- 122 unit and contract tests passed, including 1 new regression test covering the fixes above.
- Swift debug build passed.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約テスト122件（上記修正を検証する新規回帰テスト1件を含む）に成功しました。
- Swiftのdebugビルドに成功しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.2.0.dmg`):

`f1b6297aee7b49333e3918aa26d98494ef1e08b7b15ac01a4fd61674d5ca29f9`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
