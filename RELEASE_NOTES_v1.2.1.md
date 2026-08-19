# MLXBar v1.2.1

MLXBar v1.2.1 is a follow-up bug-fix release addressing two issues found in v1.2.0. Upgrading is recommended for everyone, especially anyone using tool calling (e.g. ZCode) with a reasoning model such as Laguna S 2.1.

MLXBar v1.2.1は、v1.2.0で見つかった2件の不具合を追加で修正したリリースです。特に、ZCodeなどでtool callingを使いながらLaguna S 2.1のような推論モデルを利用している方には更新を推奨します。

## Fixes

- **Using Laguna S 2.1 through an OpenAI-compatible client with tool calling (e.g. ZCode) could fail with `GENERATION_FAILED`.** The chat-template step in `Workers/mlx_vlm_worker/adapter.py` swallowed any failure unconditionally and fell through to sending the raw, untemplated message list straight to `stream_generate`. The tokenizer then rejected it with the confusing `text input must be of type str...` error. The adapter now retries narrowly (dropping `tool_choice`) only on the specific `TypeError` that means the template doesn't accept it; any other failure now surfaces as a real, readable error instead of corrupting the prompt.
- **Quick Chat's prompt box could not receive keyboard input, and the Settings window could appear clipped or sluggish.** MLXBar has no Dock icon (it's a menu-bar-only, `LSUIElement` app), so opening a secondary window from the menu bar's popover did not reliably make the app — or that window — key. Keystrokes went to whatever app was previously frontmost, and an unfocused window's SwiftUI layout could render incompletely. The app now explicitly activates itself when opening the Models, Quick Chat, and Settings windows.

## 修正

- **ZCodeなどOpenAI互換クライアントからtool callingを使ってLaguna S 2.1を利用すると、`GENERATION_FAILED`になることがありました。** `Workers/mlx_vlm_worker/adapter.py`のチャットテンプレート適用処理が、失敗を無条件に握りつぶし、未加工のメッセージ配列をそのまま`stream_generate`へ渡していました。トークナイザーがこれを拒否し、分かりにくい`text input must be of type str...`エラーになっていました。現在は、テンプレートが`tool_choice`を受け付けないことが原因の`TypeError`だけを狭く拾って再試行し、それ以外の失敗はプロンプトを壊さず、そのまま分かりやすいエラーとして返します
- **クイックチャットの入力欄に文字が入力できず、設定画面が見切れたり、もっさりして見えたりすることがありました。** MLXBarはDockアイコンを持たないメニューバー常駐アプリ（`LSUIElement`）のため、メニューバーのポップアップから別ウィンドウを開いても、アプリやそのウィンドウが確実にキー状態にはなっていませんでした。キーボード入力は直前にアクティブだった別のアプリへ流れ、キーウィンドウでないSwiftUIの描画は中途半端になることがありました。モデル選択・クイックチャット・設定の各ウィンドウを開く際に、アプリを明示的にアクティブ化するようにしました

## Verification / 検証結果

- 124 unit and contract tests passed, including 2 new regression tests covering the mlx-vlm chat-template fallback fix.
- Swift debug build passed.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約テスト124件（mlx-vlmチャットテンプレートのフォールバック修正を検証する新規回帰テスト2件を含む）に成功しました。
- Swiftのdebugビルドに成功しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.2.1.dmg`):

`7cd2681ed6cedd83c01ff83fe5f4345d7352d4fea15204087dcf97b2ef9b7e6d`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
