# DESIGN v3.2.0 — ユーザー報告7件に対する最小差分改修

- 版数: app 3.2.0 / Coordinator `__version__` 3.2.0。`schemaVersion` は **1 のまま**（`models` へ additive 4列、旧DBをそのまま読める）。`CATALOG_CLASSIFIER_VERSION` は "3" のまま、能力列の再判定用に `CATALOG_CAPABILITIES_VERSION="1"` を追加。
- 日付: 2026-10-29
- 関連文書: `CHANGELOG.md`（[3.2.0]）、`RELEASE_NOTES_v3.2.0.md`、`TEST_PLAN_v3.2.0.md`（実機スモーク手順）、`DESIGN_v3.1.0.md`（前提版）
- 版数定数の反映（`Packaging/Info.plist` / `Coordinator/mlxbar/__init__.py` / `scripts/build-release.sh`）はリリース作業で行う。本文書作成時点のツリーは 3.1.0 表記のまま。

## 0. 方針

v3.1.0 の上に、ユーザー報告7件（ISS-1〜7）を**最小差分**で塞ぐ。既存APIの形状・既定値・GUI→API→workerのプロトコルは緩めない。niwaki の重み再構成も v3.2.0 に含める（ユーザー承認による）。モデル同梱 `*.py` は実行せず、config.json の証拠だけからリポジトリコードで再実装する。新規UI文言は日英両方を用意する。

## 1. ISS別: 原因 → 設計 → AC

### 1.1 ISS-1 赤字エラーが消えない

**原因**: `MenuBarViewModel.errorMessage` に代入する経路がポーリング（`refresh*` 14箇所）にあり、値が付け替わる限り表示が残り続けた。手動クリア以外に消える契機が無い。

**設計**: `errorMessage` に `didSet` を付け、not-nil 時に**20秒**の自動消去タスクを起動、再代入時は旧タスクを cancel（世代トークンで競合解消）。ポーリング由来の代入を無音化し、接続断は `connectionLost` へ分離して状態表示（アイコン/「サービス停止」）に寄せる。設定画面はフッターに赤バナーを持つ。実装は GUI モジュール内に閉じ、Coordinator の TCP/API/生成パスに触れない。

**AC**: AC-1-1（60秒実機観察は未実施）, AC-1-2, AC-1-3, AC-1-4。

### 1.2 ISS-2 vision とランタイム適性（VLMの誤起動）

**原因**: エンジン判定がキーワード依存で、vision 付きモデルが mlx-lm で起動され画像を黙って落としていた。auto チェーンは**最後の**エラーだけを raise するため、mlx-vlm 側の実失敗理由が消えていた（G3）。

**設計**:
- カタログに `capabilities`（vision/tools/reasoning）/ `runtimeFit` / `evidence` / `loaderId` を追加。vision は index・safetensors ヘッダの**実テンソル**で判定し、`language_model_only` と併用。engine と capabilities は独立に決める（vision=false でも mlx-vlm のテキスト経路があり得る）。
- auto は vision=true のとき mlx-lm を候補外にする。失敗時は `error.attempts[]` に全エンジンの理由を additive 添付（既存3キーは不変）。
- 明示 mlx-lm + vision は起動を許可し、`warnings` に `VISION_IMAGE_DROPPED`（日英）と evidence を返す。

**AC**: AC-2-1, AC-2-2, AC-2-2b（APIまで）, AC-2-3, AC-2-4, AC-2-5。

### 1.3 ISS-3 常駐モデルが起動時に載らない

**原因**: 起動直後の `_preload_last_model` が同一Tickの `scan_job` 完了前に走り、陳腐なカタログを見て pinned を黙ってスキップしていた。

**設計**: `serve()` が走査タスクのハンドルを保持し、API受付後・preload 直前に `await scan_task`。pinned があるときは `last_loaded_model_id` を載せない（`api_autoload_suspended` は全解放時のみ）。`status.preload = {state, modelId, reason, code, at}`（常に5キー、state は5値に正規化）で成否と理由を出す。単一アンロードで他の pinned を巻き込まない。

**AC**: AC-3-1, AC-3-2, AC-3-3, AC-3-4。

### 1.4 ISS-4 再起動直後の言語・設定

**原因**: 初回描画が既定言語（en）で走り、サーバ値の取得後に切り替わっていた。設定取得の失敗も無音だった。

**設計**: `init` で `UserDefaults("guiLanguage")` を `guiLanguage` / `AppLanguage.current` の初期値へミラーし、以後は `applyGUILanguage` に集約（サーバ値を正とする）。設定取得は 3/10/30 秒の**有界**バックオフ、復旧は一度だけバナー表示（`settingsRecoveryShownFor`）。

**AC**: AC-4-1, AC-4-2, AC-4-3, AC-4-4, AC-4-5。

### 1.5 ISS-5 niwaki（同梱ローダ）

**原因**: 外部の Qwen niwaki ビルドは tokenizer/重みの再構成が必要で、`mlx_lm.load(trust_remote_code=False)` 一発では読めない。従来は汎用の「リモートコードが必要」で終わっていた。

**設計**: 閉じたレジストリ（`Workers/mlx_vlm_worker/loaders/`、**リポジトリ内モジュールのみ** import。モデル同梱 `*.py` は実行しない）+ preflight（config/index/tokenizer/quant/arch + niwaki 固有判定）。既定 `models.bundledLoaders=false`（env は設定 true のときだけ）。未対応は割当前に `MODEL_FORMAT_UNSUPPORTED` / `MODEL_QUANT_UNSUPPORTED` / `MODEL_TOKENIZER_MISSING` / `MODEL_NEEDS_BUNDLED_LOADER` で即失敗。

**v3.2.0 の範囲**: レジストリ・検出・preflight・**重み再構成の実装まで**。`load_weights` フック（`kept_experts` 24層 / PLE 2bit ペア行128 shard / `shared_only_layers` ＋ detokenizer tokenmap拡張）をリポジトリコードで実装し、このMacの実機でロード成功を確認。

**AC**: AC-5-1, AC-5-2, AC-5-3, AC-5-4, AC-5-6。AC-5-1 は実機ロード成功で達成（長い生成の品質評価は継続）。

### 1.6 ISS-6 メモリ拒否の誤判定

**原因**: headroom を「resident + 要求」の二重計上で見ており、96 GiB 機でも単独ロードが拒否された。判定順も per-model → 全体 → pressure の固定で、退避できるのに拒否していた。

**設計**: `_admit` を per-model上限 → 全体予算 → OS pressure → **退避計画** → 回収後の実効空きで headroom、の順に再構成。`weights=size_bytes`、`anonymous=512MiB + weights×10%`、`_cold_estimate` は strict 1.35 / balanced 1.15。拒否理由をコードで分離（`MEMORY_BUDGET_TOTAL` / `MEMORY_HEADROOM_INSUFFICIENT` / `MEMORY_POOL_EXHAUSTED` / `MEMORY_POSTLOAD_EXCEEDED`）。ロード後実測超過は `MEMORY_POSTLOAD_EXCEEDED` で解放（backstop）。任意設定 `models.pool.memoryAdmission`（既定 `strict`）。

**AC**: AC-6-1, AC-6-2, AC-6-3, AC-6-4。

### 1.7 ISS-7 常駐設定が反映されない

**原因**: 設定配列の全置換 API しかなく、GUI が別画面で取得した古い配列を送って他設定を消していた。ロード済みモデルの「固定/一時」も判別できなかった。

**設計**: `PUT /api/v1/settings/models/pool/profiles/{model_id:path}` を追加（**マージ**、`:path` でスラッシュ入り modelId に対応、`keepLoaded=false` で解除）。`status.loadedModels[].profilePinned` を正系にし、`keepLoaded` は互換のため残置。GUI は `try?` を廃止して失敗を `errorMessage` へ出し、設定未取得時は操作を disable。

**AC**: AC-7-1, AC-7-3, AC-7-4, AC-7-5。AC-7-2 は**部分**（下記 §4）。

## 2. 変化しない部分

- **互換性**: 既存の GUI・CLI・API・Anthropic 互換・`/v1/completions`・`response_format`・RAG・コンテキスト圧縮の形状と契約は不変。`schemaVersion=1` を維持し、旧 `config.json` / 旧DBをそのまま読める（未分類行は `{}` / `null`）。
- **追加は additive のみ**: 任意設定キー2つ（`models.pool.memoryAdmission`, `models.bundledLoaders`）、`status.preload`、`profilePinned`、`attempts[]`、catalog 4フィールド。旧コード `MEMORY_BUDGET_EXCEEDED` は表・Anthropic マッピングに残置。
- **安全性**: 同梱ローダは既定無効・リポジトリ内モジュールのみ。preflight は割当前に失敗する。`_stop_worker` 経路は不変。エラー自動消去は GUI 内の Task に閉じる。
- **i18n**: 新規UI文言6件は `LS()` の日本語リテラル + `en.lproj/Localizable.strings` の両方に登録。エラーコードは `ErrorText.swift` に日英登録（§4 参照）。

## 3. 検証（実測）

実行系: `PYTHONPATH=Coordinator:Workers Coordinator/.venv/bin/python -m pytest`（Python 3.12.11 / pytest 8.4.2）、`swift test`（Xcode 同梱 toolchain）。

| 検証 | コマンド | 実測（2026-10-29） |
|---|---|---|
| Python 全件 | `pytest Tests -q` | **819 passed / 1 failed** |
| 失敗2件の内訳 | 同上 | `test_v2_8_generation_abort.py::test_disconnect_stops_the_generation_immediately`（切断タイミング）、`test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats`（heartbeat/tool_parse）。いずれも本変更のファイルを import しない既知の不安定テスト。EXEC 時点の実測は 815 passed / 2 failed |
| エラー契約テスト | `pytest Tests/test_v3_2_0_error_contract.py` | **7 passed**（旧5件 + 抽出検査2件） |
| 新規8ファイル | `test_v3_1_0_*.py` ×3, `test_v3_2_0_*.py` ×4, `test_v3_3_0_loader.py` | **77 passed** |
| Swift | `swift test` | **21 tests / 0 failures** |
| コード突合（WP-R1） | management.py の `"code":` 抽出 vs `ErrorText.swift` | **23/23 登録（欠落0）** |
| コード突合（契約テスト範囲） | `api/*.py` の `"code":` 抽出 vs 表 | **28/28 登録（欠落0）**、表の登録数 65 |
| 非空虚性（変異検査） | 表から1件削除したコピーでテスト関数を実行 | 削除したコードが `missing` に出ることを確認（本検査は作業ツリーを変更しない） |

## 4. 既知の制限・繰り延べ

1. **AC-5-1（niwaki の実ロード）は達成**。実機で LOAD OK・logits正常・argmax正常出力・detokenizer IndexError解消を確認。残るのは長い生成の品質評価（EOS停止・思考モード連鎖）の継続検証。
2. **AC-7-2 は部分**。GUI は PUT → `POST /models/{id}/load` で B を載せるが、**サーバの PUT はロードを起動しない**。A の自動解除も無い（API/CLI は設定のみ）。リリースノートに明記。
3. **AC-2-2b** の `warnings` は API で返るが GUI 未消費（WP-212）。
4. **AC-5-6（WP-R1）**: `api/*.py` の literal コードは全件登録。ただし worker 側（`Workers/`）と動的転送コード（`event.get("code")`）はこの走査の対象外で、契約テストの docstring に明記した。将来 `Workers` も走査対象にするかは別途判断。
5. **実機AC は未検証**: AC-0-1/0-3（実機 v2.8.1 のまま）、AC-1-1（60秒）、AC-3-1（15分）、AC-4-2（5秒遅延）、AC-5-5、AC-6-1/6-2/6-5。手順は `TEST_PLAN_v3.2.0.md`。
6. **F-02（次ラウンド）**: `refresh*` の無音化でページ別の失敗表示が減った。Runtime / RAG / Secrets / 設定再試行枯渇の理由表示は未実装。
7. **`memoryAdmission` / `bundledLoaders` は CLI/GUI 未露出**（設定ファイル / `PUT /api/v1/settings` で設定可能）。

## 5. 版数・文書の整合（AC-T-6）

- `CHANGELOG.md` に **[3.2.0] - 2026-10-29** を追加し、既存タグ相当の **[3.1.0] - 2026-10-19** を追記した（それ以前の最新は 3.0.0 で、3.1.0 の記録が抜けていた）。
- 本文書（`DESIGN_v3.2.0.md`）と `RELEASE_NOTES_v3.2.0.md` は同一版数・同一日付で相互参照する。
- v3.1.0 の DESIGN が参照する `Tests/test_v3_1_0_*.py` は本リリースで実体が存在する（3ファイル / 22件）。v3.1.0 設計時の「存在しないテストを参照」状態は解消済み。
- 版数定数（Info.plist / `__version__` / `build-release.sh`）の更新と DMG 生成はリリース作業で行う。
