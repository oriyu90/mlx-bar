# MLXBar v1.5.1

MLXBar v1.5.1 fixes a generation failure on Qwen3.5/3.8-class models: a conversation turn that branches away from the previous one failed with `GENERATION_FAILED` and `'ArraysCache' object has no attribute 'trim'`. Prompt reuse is an optimisation, so its failure now degrades to a fresh cache instead of losing the answer.

MLXBar v1.5.1は、Qwen3.5／3.8系モデルで会話が枝分かれしたターンが`GENERATION_FAILED`（`'ArraysCache' object has no attribute 'trim'`）で失敗する問題を修正します。プロンプト再利用は最適化に過ぎないため、失敗しても応答を失わず新しいキャッシュで続行します。

## What happened / 何が起きていたか

mlx-vlm rolls a retained prompt cache back to the shared prefix by calling `trim()` on every cache entry. The check that guards it asks whether each entry still holds its whole sequence, not whether it is trimmable at all. Hybrid Qwen3.5/3.8 layers use a cache type (`ArraysCache`) that reports `is_trimmable() == False` and has no `trim` method, so the guard passed and the call raised.

The rollback is only needed when the new prompt shares a *shorter* prefix than the cache already holds — that is, when the conversation branches. A turn that simply continues the previous one never triggered it, which is why the failure looked intermittent.

mlx-vlmは、保持しているプロンプトキャッシュを共通prefixまで巻き戻す際、各エントリの`trim()`を呼びます。その事前確認は「エントリが先頭から全体を保持しているか」を見ており、「そもそも巻き戻せるか」を見ていません。Qwen3.5／3.8のhybrid層が使うキャッシュ型（`ArraysCache`）は`is_trimmable()`が`False`を返し`trim`メソッドを持たないため、確認を通過してから例外になっていました。

巻き戻しが必要になるのは、新しいプロンプトがキャッシュより**短い**prefixしか共有しない場合、つまり会話が枝分かれしたときだけです。前のターンをそのまま続ける要求では発生しないため、断続的な失敗に見えていました。

## Fix / 修正

- Retries once with a fresh cache when the runtime's reuse machinery fails and nothing has been sent to the client yet.
- The failure happens before prefill, so the retry costs essentially nothing, and the disk cache is still consulted — a branching turn lands on a disk hit rather than a cold prefill.
- Genuine model errors are told apart by traceback and are still reported as-is, without a second attempt.
- Records recoveries as `reuseFailures` in `GET /api/v1/prompt-cache`.

- ランタイムの再利用機構が失敗し、まだ応答を送っていない場合に限り、新しいキャッシュで一度だけ再試行します。
- 失敗はprefillの前に起きるため再試行のコストは実質ありません。ディスクキャッシュは再試行でも参照されるため、枝分かれターンはcold prefillではなくdisk hitになります。
- 通常のモデルエラーはtracebackで区別し、再試行せず従来どおり報告します。
- 回復回数を`GET /api/v1/prompt-cache`の`reuseFailures`へ記録します。

## Not a v1.5.0 regression / v1.5.0の退行ではありません

The same three-request sequence reproduces on the v1.4.1 code. How the prompt cache is handed to mlx-vlm has not changed since v1.3.7; v1.5.0 did not touch that path.

同じ3要求の手順がv1.4.1のコードでも再現します。プロンプトキャッシュをmlx-vlmへ渡す実装はv1.3.7から変わっておらず、v1.5.0はこの経路に触れていません。

## Verification / 検証

- Reproduced on `Qwen3.5-9B-MLX-8bit`, which shares the qwen3_5 hybrid architecture with `Qwen3.8-27B-MLX-8bit`, and confirmed the same failure on the v1.4.1 code.
- After the fix all three requests succeed and the branching turns report `cache_tier=disk` with 1,174 tokens reused; `reuseFailures` is 2.
- 195 Python tests pass, including one that a genuine model error is not retried.
- Swift Debug and Release builds passed.

- `Qwen3.8-27B-MLX-8bit`と同じqwen3_5 hybrid構成の`Qwen3.5-9B-MLX-8bit`で再現し、v1.4.1のコードでも同じ失敗が起きることを確認しました。
- 修正後は3要求すべてが成功し、枝分かれターンは`cache_tier=disk`（1,174 tokens再利用）、`reuseFailures`は2でした。
- Pythonテスト195件が成功しました（通常のモデルエラーが再試行されないことの確認を含みます）。
- Swift Debug／Release buildが成功しました。

## Upstream / 上流

The root cause is in mlx-vlm 0.6.15's `generate/dispatch.py`. MLXBar only works around it; once the runtime checks `is_trimmable()` alongside its retention check, the retry will simply stop happening.

根本原因はmlx-vlm 0.6.15の`generate/dispatch.py`にあります。MLXBar側は回避策のみで、ランタイムが保持状態の確認と併せて`is_trimmable()`を見るようになれば、再試行は自然に発生しなくなります。

SHA-256 (`MLXBar-1.5.1.dmg`):

`f908ac9b1c232d70c7f2b1308f6621379dcffc0298bdeaf7ad64678017c9d6b5`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
