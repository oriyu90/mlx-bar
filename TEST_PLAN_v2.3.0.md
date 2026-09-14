# MLXBar v2.3.0 検証計画と結果

実施日: 2026-09-15

## 1. 自動回帰テスト

- [x] `Coordinator/.venv/bin/python -m pytest -q Tests -p no:randomly`
- [x] `Coordinator/.venv/bin/python -m pytest -q Tests`
- [x] Paged KV専用のunit/fault-injection test
- [x] `python -m compileall -q Coordinator/mlxbar Workers`
- [x] `git diff --check`

結果: Pythonテストは固定順・ランダム順とも **492 passed**。Paged KV専用は20 passed。

## 2. 実MLX runtimeと実モデル

- [x] 組み込みruntime（mlx-lm 0.31.3 / MLX 0.32.2）でplain cacheのstate arityをself-probe。
- [x] 2 block保存、512 token復元、offset/value一致、分岐時の256 token復元、namespace分離を確認。
- [x] half-renamed blockの再起動なし自己修復と、headroom不足時のstore skipを確認。
- [x] Ornith-1.5-9B-MLX-8bitは `non_plain_kv_layout` / `checkpoint_only` で安全に非対応判定。

## 3. Coordinator / GUI / ビルド

- [x] 隔離した一時`MLXBAR_HOME`で設定GET/PUT、Paged専用clear、再起動後の永続化を確認。
- [x] Swift Debug / Release build、plist lint、日英ローカライズの静的完全性を確認。
- [x] master OFF時の従属control無効化、対応理由と統計、削除確認ダイアログをコードレビュ。
- [ ] メニューバー専用appのSettings windowは自動UI操作からaccessibility targetとして取得できず、
  最終の実画面目視は未実施。ビルド、実装配線、日英文言、従属状態の確認で補完した。

## 4. リリース成果物

- [x] `SKIP_DMG=1 scripts/build-release.sh`（app構造の事前検査）
- [x] `scripts/build-release.sh`
- [x] `VERSION=2.3.0 scripts/verify-release.sh`
- [x] app / DMG内の版数、ad-hoc署名、DMG SHA-256を確認。

結果: app 2.3.0 / build 41、`Signature=adhoc`、DMGは `hdiutil verify` VALID。
SHA-256は `3fdca5270d6bce9029a89e21bf392456ddb3d75395471a16e10c77e9affc10d7`。

## 5. リリース後smoke

- [ ] GitHub Releaseがdraft/prereleaseではなくLatest、DMGとSHA-256 sidecarを配布。
- [ ] `studio-rizi.pages.dev/projects/mlx-bar/` の4言語、ダウンロードURL、構造化データ、トップNEWSを確認。
