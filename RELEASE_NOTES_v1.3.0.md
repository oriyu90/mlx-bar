# MLXBar v1.3.0

MLXBar v1.3.0 grew out of investigating a real crash seen on-device right after installing v1.2.4: the background Coordinator service could get killed by macOS on first launch. That investigation led to three related improvements: `mlxbarctl` now covers nearly everything the GUI can do, the Coordinator's startup sequence is more resilient to a specific stale-registration failure mode, and "remove all data" now leaves a genuinely clean slate for reinstalling. Upgrading is recommended for everyone, especially anyone who scripts MLXBar or has hit a startup crash after updating.

MLXBar v1.3.0は、v1.2.4のリリース検証中に実機で発生した「インストール直後にバックグラウンドサービス(Coordinator)がクラッシュする」不具合の調査から生まれたリリースです。この調査を発端に、`mlxbarctl`(CLI)からGUIとほぼ同等の操作ができるようにし、Coordinatorの起動シーケンスを特定のスタール登録の失敗パターンに対して堅牢化し、「すべてのデータを削除」が再インストール時に本当にクリーンな状態になるようにしました。全員に更新を推奨します。特にMLXBarをスクリプトから操作している方、更新後に起動時クラッシュに遭遇した方は優先してご検討ください。

## What's new

- **`mlxbarctl` now covers nearly everything the GUI can do.** New commands: `cancel-all`, `runtime delete-slot`/`runtime cancel-job`, `secrets get-api-token`/`set-api-token`/`regenerate-api-token`/`get-lmstudio-token`/`set-lmstudio-token`, `logs show`/`logs clear`, `network set-lan --enabled/--disabled` (a single atomic settings change, matching a validation rule the API enforces), `network set-port`, `config set-language`/`set-max-tokens`/`set-queue-limits`/`set-sampling-defaults`, `model add-folder`/`remove-folder`, and `remove-all-data --yes`. "Launch at login" can only have its *desired* setting changed from the CLI (`config set-launch-at-login`) — `SMAppService`, the actual macOS registration API, is Swift/ObjC-only with no CLI equivalent; the running GUI app reconciles the real registration to match on its next launch. This limitation is documented in the command's `--help` text.

## 追加

- **`mlxbarctl`から、これまでGUI専用だった操作の大半を実行できるようになりました。** 新規コマンド: `cancel-all`、`runtime delete-slot`/`runtime cancel-job`、`secrets get-api-token`/`set-api-token`/`regenerate-api-token`/`get-lmstudio-token`/`set-lmstudio-token`、`logs show`/`logs clear`、`network set-lan --enabled/--disabled`（APIが要求するバリデーションルールに合わせた単一のアトミックな設定変更）、`network set-port`、`config set-language`/`set-max-tokens`/`set-queue-limits`/`set-sampling-defaults`、`model add-folder`/`remove-folder`、`remove-all-data --yes`。「ログイン時に起動」だけはCLIから**希望する設定値**しか変更できません（`config set-launch-at-login`）。実際のmacOS登録API(`SMAppService`)はSwift/ObjC専用でCLIから直接呼べないため、起動中のGUIアプリが次回起動時に実際の登録を希望値へ合わせます。この制約はコマンドの`--help`に明記しています。

## Fixes

- **The background Coordinator service could get killed by macOS right after a fresh install, before the menu bar icon ever became clickable.** The crash report showed `SIGKILL (Code Signature Invalid)` / a Launch Constraint Violation. Root cause: this app is ad-hoc signed (its code-signing identity changes on every rebuild), and a previous build's LaunchAgent registration under the same label (`com.yukiorita.MLXBar.Coordinator`) left behind by an in-place replacement (rather than "Remove all data and quit" first) can collide with a new install's registration attempt. The startup sequence now includes a defensive recovery step — clearing both possible stale registrations and retrying — when the first attempt doesn't come up healthy, and detects a `.requiresApproval` Login Items state immediately on first launch instead of only on a later one.
- **A Coordinator crash could leave no record anywhere.** launchd's configuration deliberately sends stderr to `/dev/null` (a security precaution against a shared-`/tmp` symlink attack), so any exception thrown before Python's `logging` module was configured — or any exception that escaped uncaught — vanished without a trace. A crash log independent of `logging`'s own state now writes to `~/Library/Application Support/MLXBar/logs/coordinator-crash.log`.
- **"Remove all data and quit" could leave enough behind that a fresh reinstall inherited problems from the old one.** The actual data (settings, tokens, database, runtime installs, uv cache, worker sockets) is now wiped by a new Coordinator-owned endpoint (`POST /api/v1/system/reset`) instead of a hardcoded path list maintained on the Swift side — the process that actually owns its data directory is now the one responsible for clearing it, and both the GUI button and `mlxbarctl remove-all-data` call the same endpoint. Unregistering the macOS Login Items entry (`SMAppService`) still can't be done from the CLI (no Python equivalent exists), but `launchctl bootout` fully stops the service either way, so this doesn't block a clean reinstall — only a cosmetic Login Items entry can remain until `MLXBar.app` itself is deleted.
- While investigating, also found and fixed a separate startup race: the Coordinator's management API could start accepting connections before its own first-launch auto-install/scan bookkeeping had finished, and a request arriving in that narrow window could crash the coordinator. Startup bookkeeping now completes before the management API can accept any connection.
- Also found the Coordinator's own reported version (returned by `/api/v1/health`) had been stuck at 1.1.0 for three releases (v1.2.0–v1.2.4). The app's startup sequence treats an exact version match as its definition of "healthy," so this mismatch meant it may have been rebuilding the background service's registration on every single launch even when the Coordinator was already running fine — very plausibly a contributing factor to the Launch Constraint crash above, since more re-registrations meant more opportunities to collide with a stale one. Fixed, and added to the release version-bump checklist to prevent a repeat.

## 修正

- **新規インストール直後、メニューバーアイコンをクリックできるようになるより前に、バックグラウンドのCoordinatorサービスがmacOSに強制終了されることがありました。** クラッシュレポートは`SIGKILL (Code Signature Invalid)`／Launch Constraint Violationを示していました。根本原因: このアプリはad-hoc署名（再ビルドのたびに署名IDが変わる）で配布されており、「すべてのデータを削除して終了」を経ずに（インプレースの入れ替えなどで）残った過去のビルドのLaunchAgent登録が、同じラベル(`com.yukiorita.MLXBar.Coordinator`)を使う新しいインストールの登録試行と衝突することがありました。起動シーケンスに、最初の試行が健全な状態にならなかった場合の防御的な回復ステップ（両方の登録経路を掃除してから再試行）を追加し、ログイン項目の許可待ち(`.requiresApproval`)状態も、以前は次回起動まで検知できなかったものを初回起動でその場で検知するようにしました。
- **Coordinatorがクラッシュしても、どこにも記録が残らないことがありました。** launchdの設定はセキュリティ上の理由（共有`/tmp`のシンボリックリンク攻撃対策）で意図的にstderrを`/dev/null`へ送るため、Pythonの`logging`モジュールが設定される前に発生した例外や、どこにも捕捉されずに伝播した例外は、跡形もなく消えていました。`logging`自体の状態に依存しないクラッシュログを`~/Library/Application Support/MLXBar/logs/coordinator-crash.log`へ書き込むようにしました。
- **「すべてのデータを削除して終了」を実行しても、再インストール後に以前の問題を引き継ぐのに十分な残留物が残ることがありました。** データの実体（設定・トークン・データベース・ランタイム・uvキャッシュ・workerソケット）の削除を、Swift側で保持していたハードコードのパスリストではなく、そのデータディレクトリを実際に所有するCoordinator自身が担う新しいエンドポイント(`POST /api/v1/system/reset`)に一本化しました。GUIのボタンと`mlxbarctl remove-all-data`はどちらもこの同じエンドポイントを呼びます。macOSのログイン項目登録(`SMAppService`)の解除だけは今もCLIから行えません（Python側に相当するAPIが存在しないため）が、`launchctl bootout`でサービス自体は確実に停止するため、再インストールの妨げにはなりません。アプリ本体を削除するまで、システム設定に見た目上のログイン項目エントリが残るだけです。
- 調査の過程で、Coordinatorの起動シーケンスに別の競合も見つけて修正しました。管理APIがリクエストを受け付け可能になるタイミングが、起動時の自動インストール・スキャン処理の完了より早く、その狭い時間差でリクエストが届くとCoordinatorがクラッシュしうる状態でした。起動時のブックキーピング処理が、管理APIが接続を受け付け可能になる前に完了するよう順序を変更しました。
- また、Coordinatorが`/api/v1/health`で返す自身のバージョン文字列が、v1.2.0からv1.2.4までの3リリースにわたって1.1.0のまま更新されていなかったことも判明しました。アプリの起動シーケンスは、このバージョンが完全一致することを「健全」の条件としているため、この不一致により、Coordinatorが実際には正常に動作している場合でも、**起動のたびにバックグラウンドサービスの登録を毎回作り直していた**可能性があります（登録をやり直す回数が多いほど、上記のLaunch Constraint Violationに遭遇する機会も増えていたと考えられます）。修正し、以後のリリースのバージョン更新チェックリストにも追加しました。

## Verification / 検証結果

- 134 unit, contract, and integration tests passed (127 existing + 7 new for the CLI additions), including two new end-to-end tests for the reset endpoint's shutdown ordering and job-cancellation behavior.
- Swift debug build passed. Manual verification: exercised the new CLI commands against a live coordinator and confirmed each matches its GUI counterpart's behavior; ran `remove-all-data` via both the GUI and the CLI and confirmed a subsequent coordinator launch starts clean with default settings; deliberately created a stale conflicting LaunchAgent registration under the shared label and confirmed the packaged app's startup sequence recovers instead of failing.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約・結合テスト134件に成功しました（既存127件＋CLI追加分の新規7件）。resetエンドポイントのシャットダウン順序とジョブキャンセル動作を検証する結合テスト2件を新規に含みます。
- Swiftのdebugビルドに成功。実機検証として、新規CLIコマンドを実際に起動中のcoordinatorに対して実行し、対応するGUI操作と同じ結果になることを確認、GUI・CLI両方から`remove-all-data`を実行して以降の起動がデフォルト設定でクリーンに立ち上がることを確認、意図的に同じラベルで競合するLaunchAgent登録を作成した上でパッケージ化されたアプリの起動シーケンスが失敗せず回復することを確認しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.0.dmg`):

`f8b382889d3806dd8d4d9c1d01780d7695054e28d9aa5f4a2a18ce21d5538973`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
