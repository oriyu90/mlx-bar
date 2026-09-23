# MLXBar v2.4.1 設計：v2.4.0監査指摘の修正

更新日: 2026-09-23
対象: `oriyu90/mlx-bar` v2.4.0 → v2.4.1

## 1. 目的と範囲

v2.4.0実装後の安全性・完全性監査で検出した4件を修正するパッチリリース。
新規エンジン機能・新規設定キーの追加はない。既定値はすべてv2.4.0と同一。

1. **A1**: `general.logLevel` にGUI PickerとCLI（`config set-log-level`）だけがあり、
   値を読むコードが存在しないプラセボ状態だった。`main.py:server_log_level()` を新設し、
   public listenerとmanagement UDSサーバの両uvicornへ配線する。
2. **A2**: `logLevel` にサーバ側の値検証がなかった。`_validate` で
   `debug/info/warning/error` の4択に制限する（日英メッセージ付き）。
3. **B1**: メモリガード欄とAPI要求上限欄（同時接続数）に再起動要否の注意文がなかった。
   日英注意文を追加する。ログレベル欄にも起動時latchの注記を追加する。
4. **B2/B3**: MB表示の丸め（1MB未満→0MB表示）を注記する。headroom Stepperの刻み外の値は
   Swiftガードと422の二重で弾かれることを確認済みのため、コード変更はしない。

## 2. リリース不変条件

1. 既定値はすべてv2.4.0と同一。`DEFAULTS` の変更は検証追加のみで、値の変更はない。
   既存 `config.json`（`logLevel` を含む）はそのまま動作する。
2. `server_log_level()` は未知値・欠落時に `"warning"`（従来の直書き値）へ戻すため、
   配線前と同一の動作を保証する。uvicornへの不正値 전달は検証と二重で防止する。
3. ログレベルは起動時latch（listener切替時にも再読込）。実行中への即時反映はしない。
4. 日本語文字列をキーとし、英語は `en.lproj/Localizable.strings` のみに追加する
   （重複キー禁止）。
5. 公開物に秘密・絶対パス・環境固有値を含めない。署名は ad-hoc（ルール8）。

## 3. 設定と運用

- `general.logLevel` の変更は次回サービス起動時に反映される（ポート/LAN切替でも再読込）。
  management UDSサーバにも同一値を適用する。
- メモリガード比率の変更は、実行中の生成への反映にモデルの再ロードが必要
  （Worker起動時env＋watchdog生読込の混成のため）。
- `maxConcurrentConnections` の変更は次回サービス起動時に反映される
  （`maxRequestBytes` は要求ごとに再計算されるため即時）。

## 4. 検証

- `Tests/test_gui_settings_parity.py::LogLevelTests`（3件）:
  4値の受理、不正値（verbose/INFO/空/None）の拒否、
  `server_log_level()` の透過とフォールバック。
- 既存回帰の維持、`swift build`（Debug）の成功、日英 `.strings` の重複キー検査。
- 実MLX runtime・実モデルの再検証は不要（エンジン・Workerに変更なし）。
  v2.4.0の実機記録を継承する。
