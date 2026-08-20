# MLXBar v1.3.7

MLXBar v1.3.7 reduces ZCode latency on large Qwen models by reusing the common token prefix shared by consecutive text requests. ZCode can keep sending the complete OpenAI-compatible messages and tools on every turn; MLXBar preserves their semantics while avoiding repeated prefill work.

MLXBar v1.3.7は、連続するテキスト要求の共通token prefixを再利用し、大規模QwenモデルをZCodeから使う際の待ち時間を短縮します。ZCodeは完全なOpenAI互換messages・toolsを毎ターン送信でき、MLXBarは意味を変えずに重複prefillを省略します。

## Improvements

- Reuses the longest common token prefix for consecutive mlx-vlm text requests.
- Preserves OpenAI messages, tools, tool choice, thinking, streaming, and tool-call behavior.
- Never shares the text prefix cache with image requests.
- Discards partially mutated cache state after cancellation or generation failure.
- Releases the disposable prefix cache before returning a memory-pressure error.
- Records privacy-safe latency diagnostics without storing message bodies, tool definitions, responses, or API keys.
- Reports runtime prompt/completion usage, cached tokens, prompt throughput, and generation throughput.
- Falls back from OpenAI `high` / `minimal` reasoning effort to Qwen `xhigh` / `low` only when the original template render fails.

## 改善

- 連続するmlx-vlmテキスト要求で最長共通token prefixを再利用します。
- OpenAIのmessages、tools、tool choice、thinking、streaming、tool call動作を維持します。
- テキスト用prefix cacheを画像要求とは共有しません。
- キャンセルまたは生成失敗後は、途中まで変更されたキャッシュ状態を破棄します。
- メモリ圧迫エラーを返す前に、破棄可能なprefix cacheを解放して再判定します。
- メッセージ本文・tool定義・応答・APIキーを保存せず、プライバシーに配慮した遅延診断情報を記録します。
- runtime実測のprompt/completion使用量、cached tokens、prompt速度、生成速度を報告します。
- OpenAIの`high` / `minimal`推論強度をまず原値で試し、テンプレートが拒否した場合だけQwenの`xhigh` / `low`へ再試行します。

## Performance verification / 性能検証

On `Qwen3.8-27B-MLX-8bit` with 23 tools (55,881 serialized characters and 10,677 prompt tokens):

- Cold first-content latency: 35.253 seconds.
- Warm next-turn first-content latency: 0.399 seconds.
- Reused prefix: 10,688 tokens.
- Approximate first-content latency improvement: 88x.

`Qwen3.8-27B-MLX-8bit`へ23 tools（シリアライズ後55,881文字、10,677 prompt tokens）を送った実機検証結果:

- cold時の最初の本文: 35.253秒。
- 次ターンの最初の本文: 0.399秒。
- 再利用したprefix: 10,688 tokens。
- 最初の本文までの待ち時間: 約88倍改善。

The first request after loading a model, or a request without a common prefix, still requires cold prefill. This release accelerates subsequent ZCode turns without dropping tools or changing API semantics.

モデルロード後の初回要求や共通prefixがない要求ではcold prefillが必要です。本リリースはtoolsを削除したりAPI semanticsを変更したりせず、ZCodeの後続ターンを高速化します。

## Verification / 検証結果

- 156 unit, contract, and integration tests passed.
- Python compilation, the Swift release build, app signature, packaged resources, LaunchAgent, cache exclusion, and DMG structure were verified.

- 単体・契約・結合テスト156件に成功しました。
- Pythonコンパイル、Swiftリリースビルド、アプリ署名、同梱リソース、LaunchAgent、キャッシュ除外、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.7.dmg`):

`b44ece4a9dd0a0da5037351b0251890f8a4f29baed6c2556224a987ad55175c5`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
