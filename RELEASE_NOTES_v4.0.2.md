# MLXBar 4.0.2 リリースノート

**リリース日**: 2026-11-01
**ビルド**: 69
**種別**: 不具合修正（ログイン時の起動）

## 修正: 再起動時にターミナルの窓が開く問題

### 症状

Mac を再起動してログインすると、ターミナルの窓が開き、次のような表示が残る。

```
Last login: ...
user@usernoMac-Studio ~ % /Users/user/places/.../.build/arm64-apple-macosx/debug/MLXBar ; exit;
```

### 原因

「ログイン時に起動」は macOS の `SMAppService.mainApp` で登録していますが、これは
**そのとき実行中の実行ファイル**を登録します。デバッグビルド
（`swift run` や `.build/<arch>/debug/MLXBar`）で動かした状態でこの設定が有効になると、
ログイン項目が **.app ではない素の実行ファイル**を指しました。

macOS は .app でない実行ファイルを開く手段が Terminal しかないため、ログインのたびに
Terminal を起動して `…/MLXBar ; exit;` を実行していました（末尾の `; exit;` がその証拠）。
アプリ本体（`/Applications/MLXBar.app`）ではなく、古い開発用バイナリが起動していた状態です。

### 対策

- **バンドル（.app）以外からは、ログイン項目を登録しない**ようにしました。
  - 判定は `MenuBarViewModel.isAppBundle(_:)`（純粋関数・テスト済み）
  - 設定の復元処理（`reconcileLaunchAtLogin`）と、設定画面のトグル処理の**両方**でガード
  - デバッグビルドから有効にした場合は、**設定値だけ保存**し、ログに理由を残します
    （後でインストール版を起動したときに正しく登録されます）
- 設定画面の「ログイン時に起動」は、**インストール版以外では無効化**し、
  「インストールした MLXBar.app からのみ設定できます」と表示します（日英）

## すでに誤ったログイン項目がある場合

この版を入れるだけでは既存の項目は消えないため、次のどちらかで解消します。

1. **システム設定 › 一般 › ログイン項目** から、`.build/.../debug/MLXBar` を指している
   項目を削除し、MLXBar の設定で「ログイン時に起動」を入れ直す
2. ターミナルで:
   ```sh
   osascript -e 'tell application "System Events" to delete login item "MLXBar"'
   ```
   の後に、MLXBar（/Applications）の設定で「ログイン時に起動」をオンにする

## 互換性

- 設定ファイルのスキーマ（`schemaVersion=1`）は変更ありません
- API・CLI の変更なし（`general.launchAtLogin` の意味は同じ。不正な登録だけを防ぐ）
- Python 側の変更なし（このリリースは SwiftUI 側のみ）

## 検証

| 項目 | 結果 |
|---|---|
| `swift test` | **33 tests / 0 failures**（+4: バンドル判定の4ケース） |
| `pytest Tests` | **947 passed / 0 failed / 1 skipped**（変更なし） |
| `check_i18n` | 457 キー欠落 0（+1） |
| 実機 | 誤ったログイン項目（`.build/.../debug/MLXBar`）を削除済み。`/Applications` 版から登録し直し、旧式ログイン項目に MLXBar が残っていないことを確認 |

## インストール

DMG を開き、MLXBar.app を Applications へコピーしてください。
**署名について**: ad-hoc 署名です（公証なし）。アプリを入れ替えた後、外付けボリュームの
モデルを使う場合は「リムーバブルボリューム」の許可を再確認してください。
