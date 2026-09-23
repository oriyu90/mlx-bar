# MLXBar v2.4.1 検証計画と結果

実施日: 2026-09-23

## 1. 自動回帰テスト

- [x] `python -m pytest -q Tests -p no:cacheprovider`（system python 3.14）
- [x] `Tests/test_gui_settings_parity.py::LogLevelTests`（受理・拒否・配線フォールバック）
- [x] `python -m compileall -q Coordinator/mlxbar Workers`
- [x] `git diff --check`

結果: Pythonテストは **519 passed**（v2.4.0の516＋新規3）。
`Coordinator/.venv`（Python 3.12）の既知の環境依存1失敗
（`test_ttl_starts_when_a_slow_load_finishes`）は本版の変更範囲外であり、
クリーンツリー再現済みの記録（TEST_PLAN_v2.4.0.md §1）を継承する。

## 2. 実MLX runtimeと実モデル

- 本版はエンジン・Workerを変更しないため再検証は不要。v2.4.0の実機記録を継承する。
- `logLevel` 配線はuvicorn起動引数の変更のみで、生成経路・キャッシュ経路に触れない。
  起動時latchであることはコードレビュ（`server_log_level` の2箇所の呼び出し）で確認。

## 3. Coordinator / GUI / ビルド

- [x] Swift Debug build（`swift build`）成功。Release buildはbuild-release.shで実施。
- [x] 日英ローカライズの重複キー検査（新規重複なし）。
- [x] `Info.plist`（2.4.1/build 43）、`__version__`、pyproject、uv.lock、build/verifyスクリプトの版数一致を確認。
- [ ] 実画面目視は未実施（v2.4.0から継続）。ビルド、実装配線、日英文言で補完した。

## 4. リリース成果物

- [x] `scripts/build-release.sh`
- [x] `VERSION=2.4.1 scripts/verify-release.sh`
- [x] app / DMG内の版数、ad-hoc署名、DMG SHA-256を確認。

結果: app 2.4.1 / build 43、`Signature=adhoc`、DMGは `hdiutil verify` VALID。
SHA-256は `2722c048b28b41bd8ceb725fd6ec1890bb1e94ab686d38cb6cba8d4fcbf11de6`。

## 5. リリース後smoke

- [x] GitHub Releaseがdraft/prereleaseではなくLatest、DMGとSHA-256 sidecarを配布。
- [x] `studio-rizi.pages.dev/projects/mlx-bar/` の4言語、ダウンロードURL、構造化データ、トップNEWSを確認。

結果: GitHub Release `v2.4.1` はLatest（draft=false / prerelease=false）。studio-rizi
`（コミット後に記入）`のCloudflare Pages反映後、公開HTMLの `softwareVersion: 2.4.1`、
4言語DMG名、`2026.09.23`の4言語UPDATEを確認した。
