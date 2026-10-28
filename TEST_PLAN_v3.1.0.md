# TEST PLAN v3.1.0 — 起動安定性・ロード耐性・表示寿命

## 検証範囲

v3.1.0は最小差分で次の4つを入れる:

1. `Menu Bar ViewModel.errorMessage` 20秒自動消去（GUI内のみの改善）
2. `Coordinator/mlxbar/main.py` の preload 前に `scan_job` 完了待ち
3. `Workers/mlx_lm_worker/adapter.py` の remote code 再試行一回
4. `Packaging/Info.plist`, `Coordinator/mlxbar/__init__.py` の版数 3.1.0 / build 58

## 1. 単体テスト

`pytest -q`（ローカル `Tests/` 全体）を通す。以下の観点を含む:

- `Tests/test_core.py` 既存カタロジカル・setting・cli・state・worker の結合目標が回帰していない
- `Tests/test_catalog_deduplication.py` パス重複/走査統合の回帰なし
- `Tests/test_v2_10_engine_detection.py` VLM/MLX-LM判別・`detect_engine` 再導出（41件）が回帰していない
- `Tests/test_v3_1_0_gui_stability.py`（新規）
  - エラーが 表示後20秒で消える 既存のアセットが保持される（GUI側のSwift状態検証への準拠）
  - 手動クリア (`errorMessage = nil`) と cancel 対応が正しい
- `Tests/test_v3_1_0_startup.py`（新規）
  - scan 未完了時に preload が走らない（`scan_task` 待ち）
  - scan 已完了（`scan_task = None`）時に preload が変わらず走る
  - scanが失敗しても serve() の例外がCoordinatorを落とさない
- `Tests/test_v3_1_0_load_robustness.py`（新規）
  - remote code 例外を1回だけTrueにして再試行する
  - 遠回りに失敗したら従来の協調エラー型へ流す（`MODEL_INCOMPATIBLE`・`chat template`・OOM）
  - 失敗しなかったら `capabilities` を返して破壊しない

## 2. 収録時ビルド（Swift）

`swift build` 成功。警告は既存と同じ長さ以内に留まる。

## 3. リリースビルド

`scripts/build-release.sh`:

- `swift build --disable-sandbox -c release`
- Coordinator / CLIを PyInstaller --onedirで組み込み
- `dist/MLXBar-3.1.0.dmg` を生成・`hdiutil verify` 成功
- `dist/MLXBar-3.1.0.dmg.sha256` を生成

## 4. リリース

- `gh release create v3.1.0` に `MLXBar-3.1.0.dmg`・`MLXBar-3.1.0.dmg.sha256` を添付
- リリースノートは `RELEASE_NOTES_v3.1.0.md` を利用
- `main` push、tag `v3.1.0` push
- `docs`/`README` を更新（インストール先のDMG名と、v3.1.0変更点の記載）
