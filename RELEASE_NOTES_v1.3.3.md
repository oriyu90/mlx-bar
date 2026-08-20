# MLXBar v1.3.3

MLXBar v1.3.3 is a compatibility patch for ZCode and other OpenAI-compatible clients. Requests containing `extra_body.chat_template_kwargs` are now accepted and forwarded to both mlx-lm and mlx-vlm chat templates instead of failing with HTTP 400 `UNSUPPORTED_PARAMETER`.

MLXBar v1.3.3は、ZCodeなどのOpenAI互換クライアント向けの互換性修正です。`extra_body.chat_template_kwargs`を含む要求をHTTP 400 `UNSUPPORTED_PARAMETER`で拒否せず、mlx-lm・mlx-vlm両方のチャットテンプレートへ伝播します。

## Fixes

- Accepts ZCode's `{"extra_body":{"chat_template_kwargs":{"enable_thinking":false}}}` request shape on `POST /v1/chat/completions`.
- Forwards model-specific chat-template values such as `enable_thinking` and `reasoning_effort` to both MLX runtimes.
- Preserves chat-template values while progressively falling back from `tools` + `tool_choice` to templates without tool support.
- Rejects malformed input and reserved keys such as `tools`, `tool_choice`, `tokenize`, `add_generation_prompt`, and `num_images` before generation, preventing callers from overriding MLXBar-managed behavior.

## 修正

- ZCodeが`POST /v1/chat/completions`へ送る`{"extra_body":{"chat_template_kwargs":{"enable_thinking":false}}}`形式を受理します。
- `enable_thinking`や`reasoning_effort`などのモデル固有値を、両方のMLXランタイムへ伝播します。
- tool callingテンプレートの段階的フォールバック時にもチャットテンプレート値を保持します。
- 不正な入力と、`tools`、`tool_choice`、`tokenize`、`add_generation_prompt`、`num_images`などの予約キーは生成前に拒否し、MLXBarが管理する動作の上書きを防ぎます。

## Verification / 検証結果

- 138 unit, contract, and integration tests passed.
- The release build, app signature, bundled resources, LaunchAgent, packaged Coordinator/CLI, and DMG structure were verified.

- 単体・契約・結合テスト138件に成功しました。
- リリースビルド、アプリ署名、同梱リソース、LaunchAgent、Coordinator/CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.3.dmg`):

`cad320e5dd5434e3439cd5109d0f864cd9450271201fd727734f2d7946608b8f`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
