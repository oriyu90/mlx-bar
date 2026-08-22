# MLXBar v1.5.3

MLXBar v1.5.3 closes a resource-consumption hole in the public API — anyone who could reach the port could make the coordinator allocate memory without presenting a credential — and adds a live tokens-per-second readout under the menu bar's "responding" line.

MLXBar v1.5.3は、公開APIで資格情報なしにメモリを消費させられる問題を塞ぎ、メニューバーの「応答を生成中」の下に現在のトークン毎秒を表示します。

## Nothing is parsed before you are authorised / 認証前には何も解析しない

FastAPI resolves a handler's `body` parameter before the handler runs, so `authorize()` was only reached after the whole request had been read and turned into Python objects. A caller with no credential could therefore allocate memory in proportion to whatever they sent.

Measured on an isolated instance, 24 concurrent unauthenticated 40 MB requests:

| | v1.5.2 | v1.5.3 |
|---|---|---|
| Coordinator RSS | 60 MB → **1,822 MB** | 60 MB → **69 MB** |

FastAPIはハンドラ引数の`body`をハンドラ本体より先に解釈するため、`authorize()`に到達するのは要求全体を読んでPythonオブジェクトに変換した後でした。資格情報を持たない相手でも、送った分だけメモリを確保させられる状態です。隔離インスタンスで認証なし40MB要求を24並列にした実測が上の表です。

## A ceiling derived from your settings / 設定から算出する上限

A fixed body cap would be wrong: eight 25 MiB images sent as base64 data URIs is a **legal** request worth about 280 MB. The ceiling is therefore computed from the same limits the handlers enforce — `maxPromptCharacters × 4` plus `maxImages × maxImageBytes × 4/3` plus 1 MB — which is about 281 MB with the defaults.

- A `Content-Length` above the ceiling is refused with HTTP 413 without transferring the body.
- A chunked upload is cut off once the ceiling is reached; memory tracks the ceiling rather than what the client sends.
- Set `api.maxRequestBytes` explicitly to lower it if you never send images.
- `api.maxConcurrentConnections` (default 64) bounds peak memory deterministically.
- `/health` stays reachable without a token, for monitoring. Failed authentication is recorded in the API log.

固定値の上限は誤りです。25 MiBの画像8枚をbase64のdata URIで送る要求は、**正当に**約280MBへ達します。そのため上限はハンドラが強制するのと同じ設定から算出します（`maxPromptCharacters`×4 ＋ `maxImages`×`maxImageBytes`×4/3 ＋ 1MB、既定で約281MB）。上限を超える`Content-Length`は本文を転送せず413で拒否し、チャンク送信は上限到達時点で打ち切ります。画像入力を使わない場合は`api.maxRequestBytes`に明示値を設定してください。

## Tokens per second while generating / 生成中のトークン毎秒

The menu bar now shows the current rate under "Model is responding", including for requests arriving from ZCode or another machine on the LAN.

It reads the runtimes' own `generation_tps`, which both mlx-lm and mlx-vlm compute on every token and which already excludes prefill. Two things make that reliable rather than fragile:

- **It does not depend on visible output.** `Qwen3.5-9B-MLX-8bit` holds 111 tokens in its detokenizer and emits them as a single delta after 14 seconds; anything that counted deltas would show nothing at all. The measurement is driven by the generation loop instead.
- **It does not depend on the runtime keeping those fields.** If they disappear, the worker measures the rate itself. Losing them costs accuracy, never the display, and never the generation.

The runtimes' first sample divides by roughly zero and reads in the tens of thousands, so nothing is published until a few tokens have gone by. Progress never enters the OpenAI response body; it travels as an SSE keep-alive and appears in `GET /api/v1/status` as `generationTokensPerSecond`.

メニューバーの「モデルが応答を生成中」の下に、その時点の速度を表示します。ZCodeやLAN内の別PCから届いた要求でも表示されます。値はmlx-lm・mlx-vlmが各トークンで計算している`generation_tps`（prefillを除外済み）です。テキストが出ないトークンでも計測するため、111トークンを14秒ため込んでから1つの`delta`で吐く実機モデルでも表示できます。ランタイムが該当項目を失った場合はWorker側の実測へ切り替わり、表示が消えることはあっても生成には影響しません。

## Verification / 検証

- 212 Python tests pass (16 more than v1.5.2), with no deprecation warnings. Swift Debug and Release builds pass.
- The memory result above was measured on an isolated coordinator, as were the 413 behaviour, the untouched legitimate 279 MB request, and the chunked cut-off (419 MB sent, +33 MB RSS at a 10 MB ceiling).
- The rate was verified end to end on a real model whose output arrives in a single delta: progress fired every second (25 → 58 → 91 → 123 → 156 tokens at 32–34 tok/s), reached `status`, and disappeared from `status` when the generation ended. Zero progress lines reached the OpenAI response body.

- Pythonテスト212件（v1.5.2比+16件）が非推奨警告なしで成功し、Swift Debug／Release buildも成功しました。
- 上の実測値、413の挙動、正当な279MB要求が拒否されないこと、チャンク打ち切り（419MB送信・上限10MBでRSS +33MB）はいずれも隔離インスタンスで確認しています。
- トークン毎秒は、出力が1つの`delta`でまとまって届く実機モデルで通しの確認をしました。

## Known limits / 既知の範囲

The ceiling follows the largest request your settings legitimately allow, so it is large by default. An authenticated client can still ask for up to that ceiling times the connection limit; lower `api.maxRequestBytes` if you do not use image input. A chunked upload that trips the ceiling surfaces as a 422 rather than a 413 — the goal there is bounded memory, not a matching status code. The displayed rate is the runtimes' cumulative average since the first token, so it is smooth and reacts slowly to a slowdown.

上限は「設定が許す最大の正当な要求」に合わせるため既定では大きく、認証済みクライアントは上限×同時接続数までメモリを要求できます。画像入力を使わない場合は`api.maxRequestBytes`を下げてください。チャンク送信の打ち切りは413ではなく422として現れます（目的はメモリの上限であり、状態コードの一致ではありません）。表示される速度は最初のトークンからの累積平均のため、滑らかである一方で速度低下への反応は緩やかです。

SHA-256 (`MLXBar-1.5.3.dmg`):

`9df5dcae6f574e2e9a48a021de1c72b6247fd6d4ce319ef379df94931334217e`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
