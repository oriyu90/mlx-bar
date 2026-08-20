# MLXBar v1.3.4

MLXBar v1.3.4 fixes ZCode compatibility for reasoning models such as Qwen3.8. Top-level `thinking` requests no longer fail with HTTP 400 `UNSUPPORTED_PARAMETER`, and `reasoning_effort` is now forwarded to the MLX chat template instead of being silently ignored.

MLXBar v1.3.4は、Qwen3.8などの推論モデルに対するZCode互換性を修正します。トップレベルの`thinking`要求をHTTP 400 `UNSUPPORTED_PARAMETER`で拒否せず、従来は実質的に無視していた`reasoning_effort`もMLXチャットテンプレートへ伝播します。

## Fixes

- Accepts top-level `thinking` as either a boolean or an object on `POST /v1/chat/completions`.
- Converts `thinking.type` to `enable_thinking`, `budget_tokens` to `thinking_budget`, and `clear_thinking` to the inverse `preserve_thinking` value.
- Forwards top-level `reasoning_effort` to both mlx-lm and mlx-vlm chat templates; `none` disables thinking when no more explicit thinking value is present.
- Gives existing `extra_body.chat_template_kwargs` values precedence when ZCode sends both request formats.
- Rejects malformed thinking configuration before loading a model.

## 修正

- `POST /v1/chat/completions`のトップレベル`thinking`をブール値またはオブジェクトとして受理します。
- `thinking.type`を`enable_thinking`、`budget_tokens`を`thinking_budget`、`clear_thinking`を逆値の`preserve_thinking`へ変換します。
- トップレベルの`reasoning_effort`をmlx-lm・mlx-vlm両方のチャットテンプレートへ伝播し、より明示的なthinking値がない場合の`none`はthinkingを無効にします。
- ZCodeが新旧両方の形式を送った場合は、既存の`extra_body.chat_template_kwargs`による値を優先します。
- 不正なthinking設定はモデルをロードする前に入力エラーとして終了します。

## Verification / 検証結果

- 142 unit, contract, and integration tests passed.
- The release build, app signature, bundled resources, LaunchAgent, packaged Coordinator/CLI, and DMG structure were verified.

- 単体・契約・結合テスト142件に成功しました。
- リリースビルド、アプリ署名、同梱リソース、LaunchAgent、Coordinator/CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.4.dmg`):

`87d6b489a38e76a5d7416a5dc18a039dd267ae7d2efad40b0fe9f6e4cbb7aae7`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
