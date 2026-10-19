# MLXBar v3.0.0 テスト計画・実施記録

**版数**: app 3.0.0 / build 56
**対象**: v2.10.0設計レビュー（7観点精査）に基づく修正 — 明示エンジン切替の実行・表示文言・None導出・ループ分離・子プロセス確実終了・autoConnect契約・oMLX常駐計上・表示翻訳・デッドコード
**作成日**: 2026-10-19
**実施環境**: Apple Silicon Mac / Python 3.12 (`Coordinator/.venv`) / Swift 6 toolchain

---

## 1. 方針

- 修正は回帰固定を **新設テストファイル** に集約する。
  - `Tests/test_v2_10_1_fixes.py`（21件）
- 既存全件（v2.10.0含む721件）との合算で `pytest -q ../Tests -p no:randomly` を1発実行。
- GUIは `swift build` の型整合＋カード内の翻訳map文言（表示面）で担保し、操作面は実機確認（§4）。
- リグレッション対象: v2.10.0の機能（ガード・明示選択・oMLXセットアップ・autoConnect・jobs cancel）が壊れていないことを721件で確認。

## 2. 新規テスト（21件）

| クラス | 件数 | 検証内容 |
|---|---|---|
| `ExplicitEngineSwitchTests` | 4 | ready スロットへの明示 `mlx-vlm` 指定が退避＋再ロードで実効化（`explicit=True` 渡り・スロット1個・実loadedが 要求エンジン）/ auto要求はreadyスロット無churn（ロード関数非呼出）/ leases>0 で `ENGINE_BUSY`（旧loaded維持）/ 同一エンジン明示はno-op |
| `SelfHealThreadTests` | 2 | 自己修復classificationが **Main Thread 以外** で実行される実測＋書込結果（engine/format/modalities）/ classification失敗がロード結果へ漏れない（旧engineのまま） |
| `SupervisorDeriveAndPhaseTests` | 5 | engine=None行がconfigから `mlx-lm` 導出／読めないパスは従来どおり「利用可能なエンジンがありません」409（`_start_worker` 非呼出）/ explicit は導出なし / フォールバック フェーズ文言が実際の切替先（mlx-vlm→自考 "mlx-lmへ切り替え中"、逆方向 "mlx-vlmへ切り替え中"）を `_start_worker` 時点で実測 |
| `EnsureOmlxBackgroundTests` | 3 | 実行中 enabled ON で `_status_update_task` 迅速開始・再呼出し idempotent（同一タスク）/ disabled は開始しない / `_get_omlx_memory_usage_cached()` 経由でも開始（resolve済み_ensure idempotent） |
| `AutoConnectRetryTests` | 4 | raise×3="3試行（契約継続）/ 成功 1回=1試行で停止 / 未到達fold 4回=4試行 / autoConnect 無効時タスクなし |
| `CommandRunnerTearDownTests` | 3 | oMLXセットアップ・ランタイム更新の `_run_command`/_command で heartbeat raise 時に子 `/bin/sleep` が terminate（returncode 実測）/ timeout 経路で `OMLX_COMMAND_TIMEOUT` 翻訳維持 |

## 3. 実施記録

- `cd Coordinator && .venv/bin/python -m pytest -q ../Tests -p no:randomly` → **742 passed**（32s）
- `swift build` → 成功（7.1s）
- `plutil -lint Packaging/Info.plist` → OK（3.0.0 / 56）
- `sh scripts/build-release.sh` → 成功（`dist/MLXBar-3.0.0.dmg` + `.sha256`）
- `sh scripts/verify-release.sh` → OK（実行バイナリ/リソース/codesign/plists）
- `hdiutil verify dist/MLXBar-3.0.0.dmg` → VALID
- 実mount確認: `CFBundleShortVersionString=3.0.0` / `CFBundleVersion=56`

## 4. 実機確認

- `/Applications/MLXBar.app` 入替後: menus ＋ Settings/Runtime のoMLXカード・モデル一覧のエンジン指定メニューは v2.10.0 のまま機能し、ロード済みモデルに対する明示切替が即時切替となること（生成中は `ENGINE_BUSY` のGUIエラー表示になること）を確認
- `omlx status` で canonical 応答、`health` 200、`setup/requirements` 200（probe cache後は展開）
- English UI でジョブメッセージ・cancel表示が翻訳されること（「Operation cancelled by the user」等）

## 5. 初出情報

検出エラーコードは v2.10.0 の15種（`OMLX_*` 9種＋本来系）から変更なし。新規コードの追加なし（`ENGINE_BUSY` は既存）。
