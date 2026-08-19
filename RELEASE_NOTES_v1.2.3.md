# MLXBar v1.2.3

MLXBar v1.2.3 fixes three issues reported by users: an overflowing Settings layout, tool-calling requests failing on templates that don't support tools, and leftover disk usage after "Remove all data and quit." Upgrading is recommended for everyone.

MLXBar v1.2.3は、利用者から報告された3件の不具合を修正したリリースです：設定画面のレイアウト崩れ、tools非対応テンプレートでのツール呼び出し失敗、「すべてのデータを削除して終了」後の残留ディスク使用量。更新を推奨します。

## Fixes

- **The Settings "Models" tab's generation-parameter sliders (Temperature, Top P, Repetition Penalty) could grow without bound and stick out past the window edge.** `LabeledContent`'s shared alignment column broke down under the unconstrained slider row, which also pushed long labels (especially in the English UI) off the left edge and let caption text run past the window without wrapping. Each parameter row is now a self-contained, width-capped block with the label above the field and slider; captions wrap within an explicit leading-aligned frame instead. `.fixedSize(horizontal:vertical:)` was deliberately avoided, since it reproduces the "sidebar renders blank" bug already documented from v1.2.2 (see [mlx-bar.md](mlx-bar.md)). The Settings Form also didn't scroll inside the window; it's now wrapped in an explicit `ScrollView`.
- **Requests with tool calling could fail to get a response on some models.** v1.2.1 narrowed the chat-template retry to only catch the `TypeError` raised when a template rejects `tool_choice`. That left templates with no tool support at all uncaught: they raise inside Jinja2 rendering (not a `TypeError`), which failed the whole generation. Template rendering now falls back in stages — `tools` + `tool_choice`, then `tools` alone, then a plain template — shared between the mlx-lm and mlx-vlm workers via `Workers/common/tool_calls.py`. Failures unrelated to tools still surface as before.
- **"Remove all data and quit" could leave the package cache used to install mlx-lm/mlx-vlm behind.** Runtime installs used `uv`'s shared cache at `~/.cache/uv`, outside MLXBar's own data folder, so it wasn't touched by data removal. Installs now use a cache scoped to `Application Support/MLXBar/uv-cache`, which is removed along with everything else. While fixing this we also found the coordinator could exit without waiting for an in-flight install job, orphaning its `uv` subprocess; shutdown now cancels any running job first.

## 修正

- **設定画面「モデル」タブの生成パラメータ（温度・Top P・繰り返しペナルティ）のスライダーが幅の制限なく伸び、ウィンドウからはみ出すことがありました。** `LabeledContent`が全行で共有する配置カラムの計算が、幅無制限のスライダー行によって崩れ、長いラベル（特に英語UI）が左端で欠けたり、説明文がウィンドウ端で折り返さず切れたりしていました。パラメータ行をラベル・入力欄・スライダーが縦に並ぶ独立したブロックへ再構成し、幅を明示的に制限。説明文も折り返すよう修正しました。`.fixedSize(horizontal:vertical:)`は、v1.2.2で確認済みの「サイドバーが空白表示になる」不具合を誘発するため意図的に使用していません（詳細は[mlx-bar.md](mlx-bar.md)）。設定画面のFormがウィンドウ内でスクロールしていなかった問題も、明示的な`ScrollView`で解消しました
- **モデルによっては、ツール呼び出し付きのリクエストで応答が返らなくなることがありました。** v1.2.1で、チャットテンプレート適用時の例外処理を「`tool_choice`非対応による`TypeError`」だけに絞ったため、`tools`自体に対応していないテンプレートがJinja2側で送出する例外（`TypeError`ではない）が未処理のまま生成全体を失敗させていました。`tools`＋`tool_choice` → `tools`のみ → プレーンなテンプレート、の順に段階的にフォールバックする処理を`Workers/common/tool_calls.py`に共通化し、mlx-lm・mlx-vlm両ワーカーで共有します。tools関連以外の失敗は従来通りそのまま表面化します
- **「すべてのデータを削除して終了」を実行しても、mlx-lm・mlx-vlmのインストールに使ったパッケージキャッシュが残ることがありました。** ランタイムインストールに使う`uv`のキャッシュがユーザー共有の`~/.cache/uv`（MLXBar専用フォルダの外）に書き込まれており、削除対象になっていませんでした。MLXBar専有の`Application Support/MLXBar/uv-cache`を使うよう変更し、削除対象に含めました。この修正の過程で、Coordinatorがインストール中のジョブを待たずに終了する場合があり、`uv`の子プロセスが孤児化しうる不具合も見つかったため、終了時に実行中のジョブを確実にキャンセルするよう修正しました

## Verification / 検証結果

- 125 unit, contract, and integration tests passed, including a new regression test for the tool-template fallback and a flaky-integration-test fix uncovered while verifying the uv-cache change (an orphaned subprocess intermittently raced the test's own temp-directory cleanup).
- Swift debug and release builds passed.
- On-device verification: rebuilt and relaunched the app repeatedly, confirmed the Settings window's Models tab no longer overflows horizontally or vertically at both the default window size and with the English locale, confirmed the sidebar renders reliably, and confirmed the app gains keyboard focus correctly when opened as a packaged `.app` (not a bare debug binary).
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約・結合テスト125件に成功しました。tools フォールバックの新規回帰テストと、uv-cacheの変更を検証する過程で見つかった結合テストの不安定性（孤児化したサブプロセスがテスト自身の一時ディレクトリ削除と稀に競合する）の修正を含みます。
- Swiftのdebug・releaseビルドに成功しました。
- 実機検証: ビルドしたアプリを繰り返し起動し、設定画面「モデル」タブが既定のウィンドウサイズ・英語UIのいずれでも横・縦方向にはみ出さないこと、サイドバーが安定して表示されること、パッケージ化された`.app`として開いた場合に正しくキーボードフォーカスを得ること（裸のdebugバイナリでは得られないことも確認）を確認しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.2.3.dmg`):

`b72b1982cb98f21708abd5bd9332111f1c3cf0719cc0643f8c2ade54decb81c7`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
