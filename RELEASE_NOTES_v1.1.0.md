# MLXBar v1.1.0

MLXBar v1.1.0 is a hardening release. It fixes the issues found in a UI, stability, and security audit of v1.0.0. Upgrading is recommended for everyone, and required for anyone who enables LAN access.

MLXBar v1.1.0は、v1.0.0のUI・安定性・安全性の監査で見つかった問題をまとめて修正した安定化リリースです。すべての利用者に更新を推奨します。LAN公開を有効にしている場合は必ず更新してください。

## Security

- **Untrusted image references are no longer passed through.** The OpenAI-compatible API accepted any `image_url` value and forwarded it to the vision runtime unchanged. A caller could name a local file path or an arbitrary URL and read its content back through the model's description. `image_url` now accepts data URIs (base64) only; local paths and `file://` are refused.
- **Remote image fetching is opt-in and SSRF-guarded.** When explicitly enabled in Settings > API Server, MLXBar resolves the host first and refuses loopback, private, link-local, reserved, and multicast addresses, and does not follow redirects.
- Accepted images are written to a mode-0700 temporary directory as mode-0600 files, passed to the worker by path only, and deleted when generation ends.
- **Coordinator logs left world-writable `/tmp`.** The launch agent no longer redirects output to a fixed `/tmp` path that another local account could pre-create as a symlink. The coordinator writes its own mode-0600 log under Application Support.
- Library-validation and JIT relaxations are now scoped to the bundled Python runtimes that need them; the SwiftUI app is signed under the hardened runtime's defaults.
- The worker socket is restricted to mode 0600 when it is created rather than at shutdown.

## Stability

- **Workers no longer survive a coordinator crash.** A worker holding a multi-GB model could outlive an abruptly killed coordinator and keep its socket bound, accumulating across launchd restarts. Startup now detects and stops a worker left by a previous run and clears stale sockets.
- SIGTERM and SIGINT are handled, so logout and `launchctl bootout` shut the worker down instead of stranding it.
- **Worker stderr goes to a log file instead of an undrained pipe.** Nothing read that pipe once the worker was serving, so a chatty runtime could fill the OS buffer and block generation mid-stream with no crash signal.
- Runtime updates have hard timeouts (1 hour for install, 10 minutes otherwise). A stalled download can no longer leave a job "running" for the coordinator's lifetime.
- A model whose worker died while idle (memory pressure, force quit) no longer keeps reporting as loaded.
- The generation-cancel registry is bounded and expires stale entries.

## Interface

- **The language switch now works everywhere.** Translations shipped in the package resource bundle, which SwiftUI's implicit lookup never searched, so choosing English left most buttons, menus, table headers, and dialogs in Japanese.
- Management API calls have timeouts. A wedged coordinator no longer leaves every control disabled behind a spinner with no way back short of relaunching.
- Streaming no longer blocks a cooperative thread for the whole generation, and stderr is drained alongside stdout, removing a potential deadlock.
- A transient error banner is cleared once a later refresh succeeds.
- Overlapping status polls can no longer let a slow response overwrite newer state.
- Requesting a file picker while another is open reports why instead of doing nothing.
- The model list shows a loading state and an empty state.
- VoiceOver labels and hints were added to the connection URL and several controls.

## セキュリティ

- **信頼できない画像参照をそのまま渡さなくなりました。** OpenAI互換APIは`image_url`の値を検証せずvisionランタイムへ転送していたため、ローカルのファイルパスや任意のURLを指定して、その内容をモデルの説明経由で読み出せる可能性がありました。`image_url`はdata URI（base64）のみを受け付け、ローカルパスと`file://`は拒否します。
- **外部URLの取得は明示的な有効化が必要で、SSRF対策を行います。** 「Settings > API Server」で有効にした場合も、ホストを解決したうえでループバック・プライベート・リンクローカル・予約済み・マルチキャストのアドレスは拒否し、リダイレクトも追跡しません。
- 受け付けた画像は権限0700の一時ディレクトリへ権限0600で書き出し、Workerへはパスのみを渡し、生成終了時に削除します。
- **Coordinatorのログが誰でも書き込める`/tmp`にありました。** LaunchAgentによる`/tmp`固定パスへの出力を廃止しました（同じMacの別アカウントがシンボリックリンクを事前作成できる状態でした）。Coordinator自身がApplication Support配下へ権限0600で書き込みます。
- ライブラリ検証の無効化とJIT許可を、必要とする同梱Pythonランタイムのみに限定しました。SwiftUIアプリ本体はハードened runtimeの既定で署名されます。
- Workerのソケット権限を、シャットダウン時ではなく作成直後に0600へ設定します。

## 安定性

- **CoordinatorのクラッシュでWorkerが残らなくなりました。** 数GBのモデルを保持したWorkerが、強制終了されたCoordinatorより長く生き残り、ソケットを占有したままlaunchdの再起動ごとに蓄積する可能性がありました。起動時に前回のWorkerを検出して停止し、残存ソケットを整理します。
- SIGTERM／SIGINTを処理するようになり、ログアウトや`launchctl bootout`でもWorkerを残しません。
- **Workerの標準エラー出力をログファイルへ切り替えました。** 稼働開始後は誰もそのパイプを読まないため、出力の多いランタイムではOSバッファが埋まり、生成が無応答のまま停止する可能性がありました。
- ランタイム更新にハードタイムアウトを追加しました（インストール1時間、その他10分）。停滞したダウンロードがジョブを無期限に「実行中」のまま残すことはありません。
- アイドル中にWorkerが停止した場合（メモリ不足、強制終了など）、「ロード済み」表示が残らなくなりました。
- 生成キャンセル記録に件数と保持期間の上限を設けました。

## インターフェース

- **言語切替が全画面で機能するようになりました。** 翻訳データはパッケージのリソースバンドルにありましたが、SwiftUIの既定の参照先には含まれないため、英語を選んでもボタン・メニュー・表見出し・ダイアログの大半が日本語のままでした。
- 管理API呼び出しにタイムアウトを追加しました。サービスが無応答になっても、すべての操作が無効のまま復帰できなくなることはありません。
- ストリーミングが生成中ずっとスレッドを占有しないようになり、標準エラー出力も同時に読み出すためデッドロックの可能性を解消しました。
- 一時的なエラー表示が、その後の再取得成功後も残り続ける問題を修正しました。
- 状態取得が重なった際に、古い応答が新しい状態を上書きしないようにしました。
- 別のファイル選択画面が開いている状態での選択操作が、無反応ではなく理由を表示するようになりました。
- モデル一覧に読み込み中表示と、モデルが無い場合の案内を追加しました。
- 接続URLなどにVoiceOver向けのラベルと説明を追加しました。

## Verification / 検証結果

- 121 unit and contract tests passed, including 28 new regression tests covering the fixes above.
- Swift debug and release builds passed under Swift 6 strict concurrency.
- End-to-end checks against the packaged build: management and public health, rejection of SSRF, local-path, and `file://` image references, unauthenticated request rejection, clean SIGTERM shutdown with no stranded socket or worker manifest, and mode-0600 log creation.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約テスト121件（上記修正を検証する新規回帰テスト28件を含む）に成功しました。
- Swift 6のstrict concurrencyでdebug／releaseビルドに成功しました。
- パッケージ済みビルドに対する統合確認：管理API／公開APIのヘルス、SSRF・ローカルパス・`file://`の画像参照の拒否、未認証リクエストの拒否、SIGTERMでのソケットとWorkerマニフェストを残さない終了、権限0600のログ作成。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.1.0.dmg`):

`5a87023ad0ef4620f7622ceeed1c0ec33260b5c043d29b19ab80e2e2cc0b500f`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
