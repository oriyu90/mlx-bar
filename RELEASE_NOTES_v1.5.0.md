# MLXBar v1.5.0

MLXBar v1.5.0 is a hardening release for running a large model all day. It came out of a design review aimed at one question: what breaks when a 27B-class model serves ZCode continuously? The answer covered memory safety, crash safety, prompt-cache efficiency, and OpenAI compatibility, and this release addresses all four.

MLXBar v1.5.0は、大規模モデルを常用するための強化リリースです。「27BクラスのモデルでZCodeを長時間動かしたとき何が壊れるか」という観点の設計精査から生まれ、メモリ安全性・クラッシュ安全性・プロンプトキャッシュ効率・OpenAI互換性の4面に対応しました。

## Highlights / 主な変更

### Prompt caching for text-only models / テキスト専用モデルのプロンプトキャッシュ

- mlx-lm models now get the two-tier prompt cache that only mlx-vlm had: mlx-lm's own `LRUPromptCache` in memory, plus a persisted prefix snapshot that survives a worker restart.
- Measured on `llm-jp-4-32b-a3b-thinking-4bit` with a 23-tool, 8,024-token ZCode-shaped prompt: 5.563 s cold, 0.051 s on a memory hit, 0.116 s with a changed final message, 0.673 s on the first request after a worker restart.
- OpenAI `messages`, `tools`, and tool-calling semantics are unchanged.

- これまでmlx-vlm経路にしかなかった二層プロンプトキャッシュを、mlx-lmモデルにも追加しました。RAM層はmlx-lm公式の`LRUPromptCache`、ディスク層はWorker再起動後も残るprefix snapshotです。
- 実機`llm-jp-4-32b-a3b-thinking-4bit`で23 tools・8,024 tokenのZCode形式入力を測定：cold 5.563秒、RAM hit 0.051秒、最初の質問を変更して0.116秒、Worker再起動後の最初の要求で0.673秒。
- OpenAIの`messages`・`tools`・tool calling動作は変更していません。

### Memory safety / メモリ安全性

- Sets MLX's wired and cache limits at load time so a large model's weights are not paged out under pressure.
- Watches memory *during* generation, not only before it, and stops the single offending request instead of letting the OS kill the worker.
- Judges pressure on free memory, macOS's own pressure level, and the process's current resident size — not only a ratio against total RAM, which cannot see the rest of the machine.

- モデルロード時にMLXのwired limitとcache limitを設定し、大きなモデルの重みがページアウトされないようにしました。
- 生成の前だけでなく生成中もメモリを監視し、Workerごと強制終了される前に該当要求だけを停止します。
- 物理メモリ総量比だけでなく、空きメモリ・macOS自身のメモリ逼迫レベル・プロセスの現在の常駐サイズでも判定します。

### Crash safety / クラッシュ安全性

- A disconnected client now always closes the worker's generator, on the MLX thread. Previously it was never closed, so the cache-discard that protects an interrupted generation did not run and the next request could reuse an inconsistent prompt cache.
- A request whose model is unloaded while it waits in the queue now gets HTTP 409 `MODEL_NOT_LOADED` and releases its generation slot immediately, instead of failing on a `None` attribute outside the block that frees the slot.
- Loading or unloading a model is refused while generations are running *or queued*, with an explicit override for the GUI.
- Exceeding the total generation timeout no longer unloads the model; only a genuinely unresponsive worker is restarted.

- クライアントが切断した場合も、Workerの生成器を必ずMLXスレッド上で閉じるようにしました。従来は閉じられず、中断された生成を守るためのキャッシュ破棄が実行されないため、次の要求が整合しないプロンプトキャッシュを再利用しうる状態でした。
- 待機中にモデルが降ろされた要求は、生成枠を解放しないまま`None`属性で失敗するのではなく、HTTP 409 `MODEL_NOT_LOADED`を返して即座に生成枠を解放します。
- 実行中または待機中の生成があるあいだはモデルのロード・アンロードを拒否し、GUIからの明示的な強制実行だけを許可します。
- 生成の安全上限を超えてもモデルをアンロードしなくなりました。再起動するのは本当に応答しないWorkerだけです。

### OpenAI compatibility / OpenAI互換性

- Returns `finish_reason: "length"` so a client can tell a truncated reply from a finished one.
- Implements `stop` sequences (detected across split token deltas) and `seed`.
- Refuses `response_format` other than `text`, and `logprobs`, with HTTP 400 instead of ignoring them silently.
- Detects every tool-call dialect the runtime parsers accept, so markup like `<|tool_call_start|>` is no longer streamed as assistant text *and* parsed into a tool call.
- Reports `tool_support: degraded` when a chat template could only be rendered by dropping `tools`.

- `finish_reason: "length"`を返すようにし、切り詰められた応答を判別できるようにしました。
- `stop`シーケンス（分割配信をまたいで検出）と`seed`に対応しました。
- `text`以外の`response_format`と`logprobs`を黙って無視せず、HTTP 400で拒否します。
- ランタイムのtool parserが解釈するすべての記法を検出するようにし、`<|tool_call_start|>`などが本文とtool callの両方で配信される問題を解消しました。
- `tools`を落とさないとテンプレートを描画できなかった場合に`tool_support: degraded`を記録します。

### Defaults and hygiene / 既定値と衛生

- **Runtime auto-update checking is now off by default.** A runtime that changes underneath a working large model invalidates the persistent prompt cache and can break a setup that was fine a moment earlier.
- Total generation timeout raised from 900 s to 3600 s; prompt-cache disk budget from 5 GB to 10 GB, with automatic collection of stale cache generations.
- A corrupt settings file still falls back to defaults, but now says so in status instead of silently resetting LAN access and port.
- The API key is cached by mtime and the API log is pruned periodically, taking two synchronous disk operations off every request.

- **ランタイムの自動更新チェックを既定で無効にしました。** 動作中の大規模モデルの下でランタイムが変わると、永続プロンプトキャッシュが無効化され、それまで動いていた構成が壊れることがあります。
- 生成の安全上限を900秒から3600秒へ、プロンプトキャッシュのディスク上限を5 GBから10 GBへ変更し、古いキャッシュ世代を自動回収します。
- 設定ファイル破損時は従来どおり既定へ戻りますが、LAN公開やポートが静かに戻るのではなく状態に記録します。
- APIキーをmtime付きでキャッシュし、APIログの剪定を一定間隔にすることで、全要求が踏んでいた同期I/Oを2件解消しました。

## Verification / 検証

- 193 Python unit, contract, integration, fault-injection, network, and concurrency tests passed (22 more than v1.4.1), with no deprecation warnings.
- The disconnect fix was verified against a real uvicorn server over a Unix domain socket, and confirmed to fail on the unpatched build.
- Prompt caching was measured end to end on a real 32B mlx-lm model across a worker restart.
- Cache-generation collection, the queued-unload path, and the current-vs-peak resident size distinction each have a regression test.
- Swift Debug and Release builds passed using Xcode 26.6.

- Pythonの単体・契約・結合・障害注入・ネットワーク・並行stressテスト193件（v1.4.1比+22件）が非推奨警告なしで成功しました。
- 切断時の修正は実uvicorn + Unixドメインソケットで検証し、未修正ビルドでは再現することも確認しました。
- プロンプトキャッシュはWorker再起動をまたいで実機32Bモデルで実測しました。
- キャッシュ世代の回収、待機中unload、常駐サイズの現在値と高水位の区別には、それぞれ回帰テストがあります。
- Xcode 26.6でSwift Debug／Release buildが成功しました。

## Upgrade notes / 更新時の注意

- `generation.totalTimeoutSeconds` moves to 3600 and `promptCache.diskMaxGB` to 10 for new installations. Existing `config.json` values are preserved.
- `runtimes.*.autoCheck` becomes `false` for new installations. If you previously relied on the setting, note that automatic checking was never implemented; updating has always been a manual action.
- New settings: `generation.wiredLimitRatio`, `generation.cacheLimitRatio`, `promptCache.keepGenerations`, `promptCache.memoryRatio`.

- 新規インストールでは`generation.totalTimeoutSeconds`が3600、`promptCache.diskMaxGB`が10になります。既存の`config.json`の値はそのまま維持されます。
- 新規インストールでは`runtimes.*.autoCheck`が`false`になります。なお自動チェックはこれまで実装されておらず、更新は常に手動操作でした。
- 追加された設定：`generation.wiredLimitRatio`、`generation.cacheLimitRatio`、`promptCache.keepGenerations`、`promptCache.memoryRatio`。

SHA-256 (`MLXBar-1.5.0.dmg`):

`cfb10c341404a38eff1e3be7820d11dd7e4ad829e262b5d90428bbeb56d77517`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
