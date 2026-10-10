# MLXBar v2.5.1 テスト計画：設定画面の表示崩れ修正

## 1. 自動テスト

- Python回帰はv2.5.0と同一内容（Swiftのみの変更のため）。`PYTHONPATH=Coordinator
  python -m pytest Tests/ -q`で全件緑（既知のasyncioタイミング失敗を除く）。
- `.strings`重複キー検査：新規キー追加なしのため重複なし（既存`モデル未ロード`残置）。

## 2. 静的・ビルド検証

- `swift build`（Debug、Swift 6 strict concurrency）成功。
- `grep fixedSize Sources/MLXBar/Settings Sources/MLXBar/Runtimes`が空
  （サイドバー空白化の罠を踏んでいないこと）。
- `sh scripts/build-release.sh`＋`VERSION=2.5.1 sh scripts/verify-release.sh`通過。
  ad-hoc署名、`codesign -v --strict` OK、DMG `hdiutil verify` VALID、
  app 2.5.1/build 45。

## 3. 実機目視（合否条件）

設定画面の全9ページ（一般／モデル／APIサーバー／LM Studio／知識ベース／
ランタイム／キャッシュ／詳細／削除）を日英両言語で確認する。

- 説明文・注意文がウィンドウ内で折り返され、右端へ突き抜けないこと。
- 行頭・アイコンが見切れていないこと。
- ウィンドウ高に収まらないページ（キャッシュ・知識ベース・モデル・APIサーバー）は
  縦スクロールで末尾まで到達できること。
- サイドバーの9項目がすべて表示・選択できること。
