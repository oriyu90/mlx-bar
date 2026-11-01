# DESIGN v4.0.2 — ログイン項目の誤登録を構造的に防ぐ

## 1. 問題

再ログイン時に Terminal の窓が開き、`.build/arm64-apple-macosx/debug/MLXBar ; exit;` が
実行されていた。ログイン項目が **.app ではない素の実行ファイル**を指していたため。

## 2. 原因

`SMAppService.mainApp.register()` は**実行中の実行ファイル**をログイン項目として登録する。
デバッグビルド（`swift run` / `.build/<arch>/debug/MLXBar`）で動かした状態で
「ログイン時に起動」が有効になると、`Bundle.main` は .app ではないため、
macOS はログイン時に Terminal でそのファイルを開く（.app 以外を開く手段が他にない）。

## 3. 設計判断

### D-1 登録はバンドルに限定する

`MenuBarViewModel.isAppBundle(_ url: URL) -> Bool`（`pathExtension == "app"`、大小無視）を
純粋関数として追加し、`isBundledApp` を `Bundle.main.bundleURL` から導出する。
登録経路は2つあるので**両方**をガードする:

1. `reconcileLaunchAtLogin()`（設定更新のたびに走る復元処理）
2. `setLaunchAtLogin(_:)`（設定画面のトグル）

非バンドル時の扱い: **設定値は保存する**（後でインストール版を起動したときに反映させるため）。
登録だけを見送り、ログに理由を残す。

### D-2 設定画面で説明する

インストール版以外ではトグルを無効化し、「インストールした MLXBar.app からのみ設定できます」
を表示する（日英）。黙って無効化すると「壊れた」と見えるため。

### D-3 既存の誤登録は自動では消さない

古いログイン項目（LSSharedFileList）をアプリから安全に削除する API は無い。
リリースノートに削除手順（システム設定／`osascript`）を明記する。

## 4. 変更点

| ファイル | 変更 |
|---|---|
| `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` | `isAppBundle` / `isBundledApp`、2つの登録経路のガード |
| `Sources/MLXBar/Settings/MLXBarSettingsView.swift` | 非バンドル時のトグル無効化＋説明 |
| `Sources/MLXBar/Resources/en.lproj/Localizable.strings` | 1キー追加 |
| `Tests/MLXBarTests/LaunchAtLoginTests.swift` | 4テスト |

## 5. リスク

| リスク | 対策 |
|---|---|
| インストール版でも登録できなくなる | 判定は `pathExtension` のみ。`/Applications/MLXBar.app` は true（テストで固定） |
| 設定値が保存されず、後で反映されない | 非バンドル時も `setConfig` で保存する |
| 既存の誤登録が残る | 文書化＋実機では削除済み |
