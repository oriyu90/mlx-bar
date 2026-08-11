# MLXBar v1.0.0

MLXBar v1.0.0 is the first stable release for Apple Silicon Macs.

MLXBar v1.0.0は、Apple Silicon Mac向けの最初の安定版です。

## Highlights

- Resizable Settings window with a larger minimum size so controls are no longer clipped.
- English is now the default GUI language; Japanese is available in Settings > General > Language.
- Runtime install, update, rollback, cancellation, history, and removal now live in Settings > Runtime.
- Missing `mlx-lm` and `mlx-vlm` runtimes are installed automatically in background jobs on startup.
- OpenAI-compatible Chat Completions streaming now uses stable chunk timestamps, one terminal chunk, consistent OpenAI-shaped validation errors, heartbeat/keep-alive behavior, and a reliable `[DONE]` terminator.

## 主な変更

- 設定ウィンドウを可変サイズ化し、コントロールが切れない最小サイズへ拡大しました。
- GUIの標準言語をEnglishに変更し、「Settings > General > Language」から日本語へ切り替えられるようにしました。
- ランタイムのインストール、更新、復元、中止、履歴、削除を「Settings > Runtime」へ統合しました。
- `mlx-lm`と`mlx-vlm`が未導入の場合、起動時にバックグラウンドで自動インストールします。
- OpenAI互換Chat Completions APIについて、ストリーム時刻の固定、終了チャンクの一意化、OpenAI形式の入力エラー、heartbeat／keep-alive、確実な`[DONE]`終了を実装しました。

## Verification

- 62 unit and contract tests passed.
- 6 coordinator end-to-end tests passed with real local listeners.
- Swift debug and release builds passed.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, DMG structure, and SHA-256 checksum passed verification.

## 検証結果

- 単体・契約テスト62件に成功しました。
- 実際のローカルリスナーを使用するCoordinator統合テスト6件に成功しました。
- Swiftのdebug／releaseビルドに成功しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造、SHA-256チェックサムを検証しました。

SHA-256 (`MLXBar-1.0.0.dmg`):

`d9c709f222387cf9b09d31d9b2596ce93f5d9073217b129a8369d1d0bec820f0`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
