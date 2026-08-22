# MLXBar v1.5.2

MLXBar v1.5.2 narrows the prompt-cache retry that v1.5.1 introduced. That retry matched too much: because `stream_generate` is defined in the very module the cache rollback lives in, every generation error looked like a cache failure, so ordinary model errors were retried and the warm cache was thrown away over an unrelated problem.

MLXBar v1.5.2は、v1.5.1で追加したプロンプトキャッシュの再試行判定を絞ります。巻き戻し処理と`stream_generate`が同じモジュールにあるため、生成中のあらゆるエラーがキャッシュ障害と判定され、通常のモデルエラーでも再試行が走り、無関係な失敗で暖まったキャッシュが捨てられていました。

## Fix / 修正

- The check now keys on the **failing call**, not the module it happened in: it reads the source of each traceback line and treats the failure as a rollback only when that line is a `.trim(` call. Reading the source rather than a line number keeps it stable across runtime versions.
- A missing-`trim` `AttributeError` still counts as a fallback for installations where the runtime source cannot be read.
- Genuine model errors surface on the first attempt again, and no longer discard the warm prompt cache.

- 判定を「失敗したモジュール」ではなく**失敗した呼び出し**へ変更しました。tracebackの各行のソースを読み、その行が`.trim(`の呼び出しである場合だけ巻き戻しの失敗と扱います。行番号ではなくソース内容を見るため、ランタイムのバージョン差に影響されません。
- ランタイムのソースを読めない構成に備え、`trim`を含むAttributeErrorも補助的な判定として残しています。
- 通常のモデルエラーは1回目でそのまま報告され、暖まったプロンプトキャッシュも破棄されません。

## Forward compatibility / 上流修正後の動作

The workaround only runs from an exception handler, so once mlx-vlm checks `is_trimmable()` alongside its retention check, it stops firing on its own. This was verified rather than assumed: with the upstream fix simulated on a real model, a continuing turn and two branching turns all succeeded at `cache_tier=disk` with 1,174 tokens reused, and `reuseFailures` stayed at 0.

回避策は例外ハンドラの中でしか動かないため、mlx-vlmが保持判定と併せて`is_trimmable()`を見るようになれば自然に発火しなくなります。これは推測ではなく実機で確認しました。上流修正を再現した状態で、継続ターンと枝分かれターン2件がすべて`cache_tier=disk`（1,174 tokens再利用）で成功し、`reuseFailures`は0のままでした。

## Verification / 検証

- Classified both failure shapes against the real runtime: the rollback failure matches, `'NoneType' object has no attribute 'config'` does not. Under v1.5.1 both matched.
- Added a regression test that places a `trim`-calling function and a non-`trim` function in the same module; it fails against the v1.5.1 check and passes against this one.
- 196 Python tests pass. Swift Debug and Release builds pass.

- 実runtimeで両方の失敗を分類し、巻き戻し失敗は一致、`'NoneType' object has no attribute 'config'`は不一致になることを確認しました。v1.5.1ではどちらも一致していました。
- 同一モジュール内に「trimを呼ぶ関数」と「呼ばない関数」を置く回帰テストを追加しました。v1.5.1の判定では失敗し、本修正で成功します。
- Pythonテスト196件、Swift Debug／Release buildが成功しました。

## Known limits / 既知の範囲

If the runtime source cannot be read, only a `trim`-shaped `AttributeError` is recognised; any other exception type from that path would fail the request as it did before v1.5.1.

ランタイムのソースを読めない場合は`trim`を含むAttributeErrorだけが判定対象になります。その経路から別の例外型が出た場合は、v1.5.1以前と同じく要求が失敗します。

SHA-256 (`MLXBar-1.5.2.dmg`):

`7c2e9cc76ac6c6ac49bb7455768862926a3c0f674de0c3536c2d432176650713`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
