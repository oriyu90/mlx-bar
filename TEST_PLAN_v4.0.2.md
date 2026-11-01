# TEST PLAN v4.0.2 — ログイン時の起動（誤登録の防止）

## 1. 自動テスト

```sh
swift test                                               # 33 tests / 0 failures
cd Coordinator && ./.venv/bin/python -m pytest ../Tests -q   # 947 passed / 0 failed / 1 skipped
./Coordinator/.venv/bin/python scripts/check_i18n.py     # 457 keys 欠落 0
```

新規: `Tests/MLXBarTests/LaunchAtLoginTests.swift`（4件）
- `/Applications/MLXBar.app` → バンドル（true）
- `/.build/arm64-apple-macosx/debug/MLXBar` → バンドルでない（false）
- `/Applications/MLXBar`（拡張子なし）→ false
- `/Applications/MLXBar.APP` → true（大文字小文字を無視）

## 2. 手動検証

### V-1 インストール版からは従来どおり登録できる
1. `/Applications/MLXBar.app` を起動
2. 設定 →「ログイン時に起動」をオン
3. **システム設定 › 一般 › ログイン項目** に MLXBar が載る（Terminal は開かない）
4. 再ログイン → メニューバーに常駐し、ターミナルの窓が出ない

### V-2 デバッグビルドからは登録できない（v4.0.2 の核心）
```sh
cd /Users/user/places/適当/work/mlx-bar
swift build && .build/arm64-apple-macosx/debug/MLXBar &
# 設定画面を開く
```
期待: 「ログイン時に起動」が**グレーアウト**し、
「インストールした MLXBar.app からのみ設定できます」と表示。
ログに `launch at login needs the installed app` が残り、**ログイン項目は作られない**。

### V-3 既存の誤登録の確認
```sh
osascript -e 'tell application "System Events" to get the path of every login item' | tr ',' '\n' | grep -i mlx
```
期待: `.build/.../debug/MLXBar` を指す項目が**無い**こと。

## 3. 回帰
- 設定 `general.launchAtLogin` の読み書きは従来どおり（API/CLI から変更可能）
- Python 側は無変更（947 passed を維持）
