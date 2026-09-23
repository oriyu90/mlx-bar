# MLXBar v2.4.0 検証計画と結果

実施日: 2026-09-23

## 1. 自動回帰テスト

- [x] `Coordinator/.venv/bin/python -m pytest -q Tests -p no:cacheprovider`
      （venvはPython 3.12。system python 3.14でも同内容を実施し比較）
- [x] GUI設定パリティ専用 `Tests/test_gui_settings_parity.py`（CLI範囲・サーバ受理契約・将来ゲート拒否）
- [x] `python -m compileall -q Coordinator/mlxbar Workers`
- [x] `git diff --check`

結果: system python 3.14では **516 passed**（ベースライン492＋新規24）。
`Coordinator/.venv`（Python 3.12）では515 passed＋1 failedで、失敗は
`test_model_pool.py::ModelPoolTests::test_ttl_starts_when_a_slow_load_finishes`
（`await asyncio.sleep(0)`後のslot登録タイミング依存のKeyError）。
クリーンツリー（`ad1d3d3`、本版変更なし）でも同一失敗することを確認済みのため、
本版由来の回帰ではなくPython版差による既存の環境依存失敗と判断し、
不具合自体には手を付けていない（次版で`asyncio.sleep(0)`の前提を見直す候補）。
新規の内訳はtest_gui_settings_parity.py 24件（CLI 11・サーバ契約 11・ゲート拒否 2）。

## 2. 実MLX runtimeと実モデル

- 本版はエンジン機能を変更しないため、実MLX runtime・実モデルの再検証は不要と判断。
  v2.3.0の実機記録（mlx-lm 0.31.3 / MLX 0.32.2、Ornith-1.5-9B fail-closed）を継承する。
- 唯一のサーバ動作変更（`hybridKVReuse: true`拒否）は純設定検証のため、
  unitテスト（受理契約・拒否）で確認。既存`test_core.py`の`false`既定アサートも維持。

## 3. Coordinator / GUI / ビルド

- [x] Swift Debug build（`swift build`）、Release build（build-release.sh経由）成功。
- [x] 日英ローカライズの重複キー検査（新規重複なし。既存`モデル未ロード`重複1件は対象外）。
- [x] `Info.plist`（2.4.0/build 42）、`__version__`、pyproject、uv.lock、build/verifyスクリプトの版数一致を確認。
- [ ] メニューバー専用appのSettings windowは自動UI操作からaccessibility targetとして取得できず、
  最終の実画面目視は未実施。ビルド、実装配線、日英文言、従属状態の確認で補完した。

## 4. リリース成果物

- [x] `scripts/build-release.sh`
- [x] `VERSION=2.4.0 scripts/verify-release.sh`
- [x] app / DMG内の版数、ad-hoc署名、DMG SHA-256を確認。

結果: app 2.4.0 / build 42、`Signature=adhoc`、DMGは `hdiutil verify` VALID。
SHA-256は `3e92cea8221f381f21f1900f73ad53190e55d3f1c3253606fea9e936a99c693d`。

## 5. リリース後smoke

- [x] GitHub Releaseがdraft/prereleaseではなくLatest、DMGとSHA-256 sidecarを配布。
- [x] `studio-rizi.pages.dev/projects/mlx-bar/` の4言語、ダウンロードURL、構造化データ、トップNEWSを確認。

結果: GitHub Release `v2.4.0` はLatest（draft=false / prerelease=false）。studio-rizi
`78945c6`のCloudflare Pages反映後、公開HTMLの `softwareVersion: 2.4.0`、
4言語DMG名、`2026.09.23`の4言語UPDATEを確認した。
