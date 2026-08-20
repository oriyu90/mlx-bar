# MLXBar v1.3.5

MLXBar v1.3.5 fixes the remaining ZCode `UNSUPPORTED_PARAMETER` failure seen with Qwen3.8 after updating to v1.3.4. OpenAI-compatible clients may add provider- or model-specific fields over time; MLXBar now safely ignores unknown compatibility extensions instead of rejecting the entire request.

MLXBar v1.3.5は、v1.3.4更新後もQwen3.8とZCodeの組み合わせで発生していた`UNSUPPORTED_PARAMETER`を修正します。OpenAI互換クライアントが追加する未知の拡張項目を、要求全体のHTTP 400エラーにせず安全に無視します。

## Fixes

- Accepts known `thinking` and `reasoning_effort` forms at the top level and inside `extra_body`.
- Converts `thinking.effort` to the Qwen chat-template `reasoning_effort` value.
- Ignores unknown client/provider extension fields without forwarding them to the model worker.
- Continues to reject reserved chat-template overrides that could conflict with MLXBar-managed tool calling.
- Records the model and error code for early input failures, and logs rejected parameter names without storing prompts, responses, or API keys.

## 修正

- トップレベルと`extra_body`内の既知の`thinking`・`reasoning_effort`形式を受理します。
- `thinking.effort`をQwenチャットテンプレート用の`reasoning_effort`へ変換します。
- 未知のクライアント・Provider拡張はモデルWorkerへ渡さず、安全に無視します。
- MLXBarが管理するtool callingと衝突する予約済みチャットテンプレート値は引き続き拒否します。
- 早期入力エラーのモデル名とコード、拒否項目名を、プロンプト・応答・APIキーを保存せず診断可能にします。

## Verification / 検証結果

- 144 unit, contract, and integration tests passed.
- The release build, app signature, bundled resources, LaunchAgent, packaged Coordinator/CLI, and DMG structure were verified.

- 単体・契約・結合テスト144件に成功しました。
- リリースビルド、アプリ署名、同梱リソース、LaunchAgent、Coordinator/CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.5.dmg`):

`57a5ba9357db39d507a42c87cf35eeb2452085b8737328d6f79ca908cce08c47`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
