# MLXBar v2.5.1 設計：設定画面の表示崩れ修正

## 1. 目的と範囲

v2.5.0の実機確認で、設定画面（Settings）に次の表示崩れがあることを確認した。
本版はこの修正のみを行い、新規機能・新規設定キー・既定値の変更はしない。

- 説明文・注意文がウィンドウ端で折り返されず、右端へ突き抜ける。
- アイコン付き行などが見切れる。
- コンテンツがウィンドウ下端で切れ、スクロールで到達できない。

## 2. 原因

いずれも保守メモ（`mlx-bar.md`「SwiftUIの既知の罠」）に記録済みの既知の挙動で、
v2.5.0で追加されたKV量子化セクション等によりキャッシュページが長大化して表面化した。

- `Form`内の素の`Text`は、`LabeledContent`と幅無制限になり得るコントロール
  （Stepper/Picker等）が同一`Form`にあると共有カラム計算が壊れ、ウィンドウ幅を
  無視して単一行で突き抜ける。折り返しには`.frame(maxWidth: .infinity,
  alignment: .leading)`を使う。`.fixedSize(horizontal: false, vertical: true)`
  は無関係のサイドバーを空白化させるため使わない（v1.2.3で実証済み）。
- 素の`Form`は`NavigationSplitView`のdetailペインで確実にスクロールしない。
  明示的に`ScrollView`で包む方式がモデルページで確立・検証済み。

## 3. 修正

- キャッシュ・知識ベース・APIサーバーページの`Form`を`ScrollView`で包む
  （モデルページと同一構造・同一コメント）。
- `Form`内の説明文・注意文・状態文のうち`.frame(maxWidth: .infinity,
  alignment: .leading)`を持たないものに一律付与する（キャッシュ／知識ベース／
  APIサーバー／一般／LM Studio／削除／詳細）。新規日本語リテラルは追加しないため
  `en.lproj`の変更は不要。`.fixedSize`はどこにも追加しない。
- ランタイムページは`List`基盤で列幅が拘束されるため対象外。
- `schemaVersion`・`DEFAULTS`・Coordinator/Worker・CLIは無変更。

## 4. 検証

`TEST_PLAN_v2.5.1.md`に記録する。`swift build`、Python回帰、実機スクリーンショット
（日英）による目視確認、DMG build/verify、ad-hoc署名確認を行う。
