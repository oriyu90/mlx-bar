# RELEASE NOTES v3.1.0

## 概要

v3.1.0は v3.0.0（安定版）の上に、ユーザー報告の5つの体験上の問題を最小差分で安定化した版です。API形状・既定値・設定スキーマは変更しません。

## 主な改善

1. **赤字エラーの20秒自動消去** — メニューバーと設定画面の`errorMessage`が表示後20秒で自動的に消えるようになりました（手動クリア時はタイマーを.cancel）。
2. **常駐モデルの起動時ロードの確実化** — Coordinator起動直後に `_preload_last_model` が走る前に `scan_job` が完了するようになりました。スキャンが未完了のカタログが原因で常駐モデルが黙ってスキップされていた問題の根処。
3. **MLX系モデルのロード耐性** — `mlx_lm_worker` のremote code必須モデルを1回まで自動再試行する機能。リモートコード無し/有りの繰り返しロードの際に、既存の`mlx-lm→mlx-vlm`フォールバックと共存します。

## 互換性

- 既存のGUI・CLI・API・Anthropic互換の形状/契約は変更ありません。
- 設定スキーマ (`schemaVersion=1`) も変わらず、既存の `config.json` がそのまま使えます。
- `CATALOG_CLASSIFIER_VERSION` は "3" のまま、v2.10.0以降の判別規則は変わりません。

## 安全性

- preloadの例外は従来通り順次継続性を破壊しない（1モデル失敗で残りも試みる）。
- 20秒Taskは GUI モジュール内のみのモジュールに留まり、CoordinatorのTCP/API/生成パスに触れない。
- remote code再試行はLoadの一回までに限り、失敗したら協調エラー型へ流す（クラッシュしない）。

## 検証

- `pytest -q`: 714パス（全緑）
- `swift build`: 成功
- `scripts/build-release.sh`: `dist/MLXBar-3.1.0.dmg`・`MLXBar-3.1.0.dmg.sha256` 生成

## おすすめ

`MLXBar-3.1.0.dmg` から置き換えてお使いください。Network/LANの設定と明示エンジン選択はv3.0.0と同じでした。
