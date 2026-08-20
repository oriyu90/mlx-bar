# MLXBar v1.3.1

MLXBar v1.3.1 is a patch release fixing a crash discovered on-device right after installing v1.3.0: clicking the menu bar icon crashed the app before the popover ever appeared. Upgrading is strongly recommended for everyone on v1.3.0 — in the affected condition, the app is unusable.

MLXBar v1.3.1は、v1.3.0のインストール直後に実機で発覚した「メニューバーのアイコンをクリックすると、ポップアップが開く前にアプリがクラッシュする」問題を修正するパッチリリースです。v1.3.0を使っている方は全員、更新を強く推奨します。該当条件下ではアプリが実質的に使用不能になります。

## Fixes

- **Clicking the menu bar icon crashed the app instantly.** `AppLanguage.bundle(for:)` in [Services/Localization.swift](Sources/MLXBar/Services/Localization.swift) resolved localization resources through SwiftPM's generated `Bundle.module` accessor, which (for an executable target) looks for `MLXBar_MLXBar.bundle` next to `Bundle.main.bundleURL` — the `.app` root. `scripts/build-release.sh` instead places that bundle under `Contents/Resources`, the same location `CoordinatorClient`'s `bundleResources` already resolves manually. The mismatch meant every attempt to localize a string on the menu bar view hit the generated accessor's `fatalError`, killing the process. The crash only fires when the GUI language isn't the source language (`"ja"`), and a fresh install defaults to `"en"`, so it reproduced reliably for most users while going unnoticed in a Japanese-language dev environment. `AppLanguage` now resolves the packaged `Contents/Resources` path first, matching `CoordinatorClient`'s convention, and only falls back to `Bundle.module` for unpackaged `swift run` builds. See [mlx-bar.md](mlx-bar.md) for the full investigation notes.

## 修正

- **メニューバーのアイコンをクリックすると即座にアプリがクラッシュしていました。** [Services/Localization.swift](Sources/MLXBar/Services/Localization.swift)の`AppLanguage.bundle(for:)`が、SwiftPM自動生成の`Bundle.module`アクセサ経由でローカライズ資材を解決していましたが、このアクセサ(executable target向け)は`MLXBar_MLXBar.bundle`を`Bundle.main.bundleURL`(`.app`直下)の隣に探す実装でした。`scripts/build-release.sh`は同バンドルを`Contents/Resources`配下に配置しており、これは`CoordinatorClient`の`bundleResources`が既に手動で解決している場所と同じです。この不一致により、メニューバーのビューで文字列をローカライズしようとするたびに生成コードの`fatalError`が発生し、プロセスが強制終了していました。GUI言語がソース言語(`"ja"`)でないときにのみ発火するクラッシュで、新規インストールの既定値は`"en"`のため、多くの利用者で確実に再現する一方、日本語の開発環境では気付かれにくい状態でした。`AppLanguage`は`CoordinatorClient`と同じ`Contents/Resources`パスを優先して解決し、`.app`化されていない`swift run`実行時のみ`Bundle.module`にフォールバックするようにしました。詳しい調査記録は[mlx-bar.md](mlx-bar.md)を参照してください。

## Verification / 検証結果

- 134 unit, contract, and integration tests passed (Coordinator, unchanged in this release).
- Swift debug and release builds passed. Manually verified on-device: built a packaged `.app`, launched it, clicked the menu bar icon, and confirmed the popover opens without a crash (previously reproduced instantly on the same machine before this fix).
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約・結合テスト134件に成功しました(Coordinatorは本リリースで変更なし)。
- Swiftのdebug・releaseビルドに成功。実機検証として、パッケージ化した`.app`を実際に起動し、メニューバーのアイコンをクリックしてポップアップがクラッシュせず開くことを確認しました(同じマシンで修正前は即座に再現していました)。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator/CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.3.1.dmg`):

`490bf582ec4370e47aa838a9fd84e079d1818ca05ddf9dc12a8eb6f13385cd85`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
