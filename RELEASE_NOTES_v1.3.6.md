# MLXBar v1.3.6

MLXBar v1.3.6 fixes a streaming stall that made ZCode appear to receive no response from Qwen models while Quick Chat worked normally. Tool-capable generations are now streamed incrementally instead of being fully buffered until completion.

MLXBar v1.3.6は、クイックチャットは動く一方、QwenモデルをZCodeから使うと応答が表示されないように見えるストリーム停止を修正します。tool calling有効時も生成全体を完了まで保持せず、通常本文とthinkingを生成中から配信します。

## Fixes

- Streams ordinary assistant text immediately when tools are available.
- Separates Qwen thinking into OpenAI-compatible `delta.reasoning_content` events.
- Detects split `<think>` and `<tool_call>` markers without exposing internal markup as normal content.
- Buffers only the tool-call portion and emits the completed call through standard `delta.tool_calls` chunks.
- Returns `TOOL_PARSE_FAILED` instead of silently ending when detected tool markup cannot be parsed.
- Prevents runtime Python bytecode caches from modifying the signed app bundle after launch.

## 修正

- toolsが利用可能な場合も通常のassistant本文を即時配信します。
- QwenのthinkingをOpenAI互換の`delta.reasoning_content`イベントへ分離します。
- 分割された`<think>`・`<tool_call>`を検出し、内部マークアップを通常本文へ漏らしません。
- tool call部分だけを保持し、確定後に標準の`delta.tool_calls`チャンクとして返します。
- 検出したtool markupを解析できない場合は、無言終了せず`TOOL_PARSE_FAILED`を返します。
- 実行時Python bytecodeキャッシュによる署名済みアプリバンドルの変更を防ぎます。

## Verification / 検証結果

- 148 unit, contract, and integration tests passed.
- The release build, app signature, bundled resources, LaunchAgent, packaged Coordinator/CLI, absence of Python cache directories, and DMG structure were verified.

- 単体・契約・結合テスト148件に成功しました。
- リリースビルド、アプリ署名、同梱リソース、LaunchAgent、Coordinator/CLI、Pythonキャッシュディレクトリがないこと、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.6.dmg`):

`c9f8deeea77c17d0030fa40061d56f1bae8fda6abe281f0d32c8bbadf03326ac`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
