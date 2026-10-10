# TEST PLAN v2.6.0 — 一括設定モード / 設定JSON入出力 / 初回セットアップ / 最新ランタイム対応

- 対象: app 2.6.0 / build 46。Coordinator 2.6.0。`schemaVersion` 1（据え置き）。
- 設計: `DESIGN_v2.6.0.md`。

## 1. 自動ゲート（必須）

- [ ] `Coordinator/.venv/bin/pytest -q Tests` が全緑。既知の失敗
      `test_model_pool.py::test_ttl_starts_when_a_slow_load_finishes`（asyncio
      タイミング依存、§0-J-6 から継続。クリーンツリーでも失敗することを確認）
      以外の失敗はリリース禁止。
- [ ] `Tests/test_v2_6_profiles.py`（34件）: hardware収集・プロファイル境界値・
      clamp範囲・effective判定・apply形状・import適用/skip行列・fresh検出・
      新CLIのPUT形状。
- [ ] `Tests/test_gui_settings_parity.py` 拡張分: preset適用payload・
      set-runtime payload・set-memory-limit保持書換の三者契約。
- [ ] `swift build` 成功。
- [ ] `.strings` 新規重複なし（既存`モデル未ロード`重複1件のみ残置）。
- [ ] `grep fixedSize` 空（新規Swiftコードの罠検査）。
- [ ] `sh scripts/build-release.sh` → `VERSION=2.6.0 sh scripts/verify-release.sh` exit 0。
- [ ] DMG `hdiutil verify` VALID、ad-hoc署名、`codesign --verify --deep --strict`。

## 2. 実機GUI目視（日英）

- [ ] 設定→一般: 一括設定モード Picker が effective（既定カスタム）を表示。
      軽量選択→確認ダイアログ→適用メッセージ＋6キー反映（`config get` 照合）。
      フル選択→同上。カスタム選択→記録のみ（設定不変）。
- [ ] 管理対象キーを手で変更（例: プール上限%）→ Picker がカスタムへ自動変化。
- [ ] 書き出し→ `.json` 保存（APIキー類なしを確認）。読み込み→
      適用件数・除外理由アラート（存在しないフォルダ・使用中ポート等）。
- [ ] ランタイムページ: 更新設定 Section（2 Toggle＋自動導入＋間隔・世代数の
      適用）。無効値（例: 0時間）はサーバー拒否メッセージ。
- [ ] 詳細ページ: 診断サマリ（版数/Python/環境/保存先）表示。
- [ ] 英語UI: 上記を `Language=English` で一通り確認（新規47キーの訳出）。
- [ ] 初回セットアップウィザード: 更新インストールでは表示されないこと
      （`setup/status required=false`）。新規分離ホームでは §3 の手順で
      表示・完了・スキップを確認。実機本番configでのウィザード表示テストは
      バックアップ→退避→復元の手順でのみ行う（§3）。

## 3. 初回セットアップの分離検証（本番configに触れない）

```bash
MLXBAR_HOME=/tmp/wiztest Coordinator/.venv/bin/python -m mlxbar.main serve --port 11499
# 別端末: UDS=/tmp/wiztest/control/coordinator.sock
curl --unix-socket $UDS http://mlxbar/api/v1/setup/status  # required=true
curl --unix-socket $UDS -X POST http://mlxbar/api/v1/setup/complete \
  -d '{"skipped":false,"choices":{"maxResidentModels":2,"preset":"light"}}'
curl --unix-socket $UDS http://mlxbar/api/v1/setup/status  # required=false
curl --unix-socket $UDS http://mlxbar/api/v1/presets/status  # effective=light
```

- [ ] required=true → complete → required=false、preset light 適用。
- [ ] skip 版: 設定不変＋completed/skipped記録。
- [ ] 既存ホーム（本番）では required=false のまま。

## 4. 最新slot実生成smoke（PyPI最新追従の検証）

環境: mlx-lm slot 20261011-015501-000（mlx-lm 0.32.0/mlx 0.32.3）、
mlx-vlm slot 20261011-015507-000（mlx-vlm 0.7.6/mlx 0.32.3）。

- [ ] mlx-lm adapter経由 8bit 実生成（SmolLM2-135M-8bit）: 出力あり、
      `kvQuant.active=true, fallbackReason=None`。
- [ ] mlx-vlm adapter経由 bf16 実生成（SmolVLM-256M）: 出力あり。
- [ ] mlx-vlm adapter経由 8bit 実生成: 出力あり、namespace `-kv-8bit` 分離、
      metrics/cached_tokens 経路正常。
- [ ] 品質注記: 256Mモデルの算数はbf16でも不正解（能力の問題）。8bitの
      品質等価はv2.5.0のlm側評価を継承し、本版は配管（kwargs受理・完走・
      分離・再試行）の検証とする。
- [ ] fault injection（初回呼出で強制失敗）: 呼出列が [kv, bf16] の2回のみ、
      `fallbackReason=first_evaluation:RuntimeError`、reuseFailures増加なし、
      二重生成なし（§2の回归: quant再試行後にreuseフォールスルーへ
      落下しないこと。if/elif/else構造で固定）。

## 5. CLI手動突き合わせ（各コマンド後に `config get` と設定画面を照合）

- [ ] `setup scan`（実機値と目視一致）、`setup status`、
      `setup run --non-interactive`（分離ホーム）、`setup run --skip`。
- [ ] `config apply-preset light/full`（`--wait`付きでscan完了）、
      `config preset-status`。
- [ ] `config export --file`／`config import --file`（他PC想定の除外表示）。
- [ ] `config set-runtime --auto-check-mlx-lm true --check-interval-hours 24`。
- [ ] `model set-memory-limit <id> 48`（他field保持を`config get`で確認）。

## 6. 公開前チェックリスト（mlx-bar.md § checklist）

- [ ] `__init__.py`・pyproject・uv.lock・Info.plist・2 scripts の版数一致
     （app 2.6.0/build 46）。
- [ ] README・CHANGELOG・RELEASE_NOTES_v2.6.0 新規。
- [ ] 紹介サイト更新＋4言語news＋HTTP検証。
- [ ] private `mlx-bar.md` §0-F 記録＋push。
- [ ] `/Applications/MLXBar.app` 入替（旧版backup）＋`diagnostics`版数確認。

## 検証結果（2026-10-11実施）

- 自動ゲート: 566 passed＋1 failed（既知flake
  `test_ttl_starts_when_a_slow_load_finishes`。クリーンツリー `7f88979`
  でも失敗することをstashで確認済み）。
- `swift build` 成功。`.strings` 新規重複なし（既存1件のみ）。
  `grep fixedSize` 空。
- DMG `MLXBar-2.6.0.dmg` SHA-256
  `77a11afd08e522a89227139b816583e6be8bac0c56c6b9f3a0a8524e7713cf1f`、
  `verify-release.sh` exit 0、`hdiutil verify` VALID、ad-hoc署名。
- slot smoke（最新slot直結）: lm-8bit完走（SmolLM2-135M、fallbackなし）／
  vlm-bf16完走／vlm-8bit完走（namespace `-kv-8bit` 分離、metrics正常）／
  fault-injectionは呼出列 [kv, bf16] の2回のみ・fallback記録・
  reuseFailures増加なし・二重生成なし。
  品質注記: 256Mモデルの算数はbf16でも不正解（能力の問題。対照実験済み）。
- 分離ホームE2E: required=true → complete(light, maxResident 3) →
  required=false・effective=light（reserve 14/perModel 24/disk 3/budget 9）。
  skip版・既存ホーム required=false も確認。本番config無接触。
- 本番GUI目視: 一般（日英・カスタム表示・折り返し・書出読込ボタン）、
  ランタイム更新設定、診断サマリ（App 2.6.0/Python 3.12.11）。
- 本番preset往復: light適用（stored/effective=light）→ 全6キー＋modeを
  元値へ復元し一致確認（budgetのint型まで復元）。
- 未実施（持越し）: ウィザードウィンドウの実画面レンダリング
  （サーバ側は分離ホームで検証済み。Swiftはbuildのみ）。次回新規
  インストール時にオーナー確認を求めること。
