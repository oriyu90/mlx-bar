# MLXBar v3.0.0 リリースノート

**リリース日**: 2026-10-19
**版数**: app 3.0.0 / build 56
**Coordinator**: 3.0.0
**位置づけ**: v2.10.0の設計レビュー（メモリ安全性・クラッシュ安全性・動作安定性・GUI完全性・設定整合性・互換性・表示正確性）に基づく安定版。

---

## 🎉 機能面（v2.10.0から引き継ぎ）

すべての v2.10.0 機能がそのまま含まれます:

1. **VLM誤起動ガード＋明示エンジン選択** — カタログ判別v3、mlx-lmへのビジョンモデル誤ロード防止、モデル一覧からの「mlx-lm/mlx-vlm で読み込む」一回限り明示指定、自動時のみロード時再導出＋1回フォールバック＋カタログ自己修復
2. **oMLXカードからのセットアップ** — ランタイムページのoMLXカードから `brew install jundot/omlx/omlx`・`omlx start`/`stop`、進捗・キャンセル付きジョブ管理、`mlxbarctl omlx install|start|stop [--wait]`
3. **oMLX autoConnect** — `models.omlx.autoConnect` 有効時のCoordinator起動時自動接続（20秒間隔・最大6回）

詳細は `RELEASE_NOTES_v2.10.0.md` を参照してください。

---

## 🛠 v3.0.0の修正（設計レビュー起点）

### 明示エンジン選択の完全性（GUI完全性／最重要）

v2.10.0では、**既にロード済みのモデル**に対する明示エンジン選択がスロットのfast-pathで握りつぶされ、結果が旧エンジンのまま返っていました（GUIでは「mlx-vlm で読み込む」を選んでもmlx-lmのまま無反応に見える）。v3.0.0では:

- 明示指定（`mlx-lm`/`mlx-vlm`）がreadyスロットの実際エンジンと異なる場合、**該当スロットを退避して要求エンジンで再ロード**
- 生成中（leases保持）は `ENGINE_BUSY` 409で明示的に拒否（生成を壊さない）
- 自動ロード時は従来どおり ready スロットを優先（無churn）

検証: `Tests/test_v2_10_1_fixes.py::ExplicitEngineSwitchTests` 4件。

### 表示の正確性

- フォールバック中の「切り替え中」表示を実際の切替先エンジン名に動的に（旧: 常に「mlx-vlmへ切り替え中」）
- 英語UIのoMLXジョブカードで「待機中」「開始しています」「完了」「失敗」「利用者が処理を中止しました」がそのまま日本語で表示される問題を翻訳
- oMLXキャッシュ更新失敗時も `serverUrl` を含むcanonical shapeを保持

検証: `SupervisorDeriveAndPhaseTests::test_fallback_phase_names_the_engine` 等2件。

### 動作安定性

- **未判別（engine未設定）モデルの自動ロード導出**: カタログ行 engine=None の自動ロードが一律409「利用可能なエンジンがありません」だったのを、読めるディレクトリからは `detect_engine` で導出してロードを試みる（読めなければ従来どおり拒否）
- **カタログ自己修復のイベントループ分離**: ロード成功後の `_refresh_catalog_classification`（config読み取り＋DB書き込み）を `asyncio.to_thread` へ移動。モデルスキャン実行中のDBロック待ちでイベントループ（ステータスポーリング・ハートビート）が停止するリスクを除去。メソッド自体は同期のまま（単体テスト互換）
- **oMLX autoConnectの再試行契約遵守**: 例外（接続系の予期しないraise）時に1回で打ち切っていたのを、6回×20秒の契約どおり継続。成功時は即終了のまま。単体テスト可能な `max_attempts`/`interval` 引数を追加

検証: `SupervisorDeriveAndPhaseTests`（None行導出2件）、`SelfHealThreadTests` 2件、`AutoConnectRetryTests` 4件。

### クラッシュ／子プロセス安全性

- **ジョブ配管の例外で子プロセスが確実に terminate** されるよう、oMLXセットアップ（`omlx_setup._run_command`）とランタイム更新（`runtimes/updater._command`）の両方に timeout／キャンセル時と同一のプロセスグループ終了を追加。progress書き込み系（DB障害等）の例外で `brew install` が取り残される余地を閉じた

検証: `CommandRunnerTearDownTests` 3件（実子プロセス `/bin/sleep` で terminate を実測）。

### メモリ安全性

- **実行中に oMLX を有効化した場合の常駐メモリ計上**: 背景のキャッシュステータス更新ループが起動時の enabled 状態でのみ開始されるため、実行中に有効化すると `_resident_charge()` がoMLX使用メモリを0とみなして予算を緩く解釈する問題を修正。charge計算・oMLXロード経路から一律 `_ensure_omlx_background()`（冪等）を呼んで背景更新を開始

検証: `EnsureOmlxBackgroundTests` 3件。

### 整合性・保守性

- Swiftの未使用デッドコード `monitoredOMLXSetupJobIDs` を除去
- その他、確認済みで問題なしと判断した領域: 設定単一キーPATCHのdeep merge・jobs.cancel の二重書き込み防止・CLI互換・`/omlx/status` canonical payload・schema（`models.omlx.autoConnect` は v2.9.0 から既定値あり）

---

## ✅ 検証記録

- pytest: **742 passed**（既存721 + 新規21）— `cd Coordinator && .venv/bin/python -m pytest -q ../Tests -p no:randomly`
- `swift build`: 成功
- `sh scripts/build-release.sh`: 成功（`dist/MLXBar-3.0.0.dmg` + `.sha256`）
- `sh scripts/verify-release.sh`: 終了コード0（manifest・codesign・plist・launchd plists）
- `plutil -lint Packaging/Info.plist`: OK（3.0.0 / build 56）
- 実配置: `/Applications/MLXBar.app` 実行中版数 3.0.0（diagnostics確認）

## 🧩 互換性

- API・CLI・設定schemaに破壊的変更なし。`models.omlx.autoConnect` のみ既存キー（v2.9.0）の実装補完
- build番号は55（v2.10.0）の直後の **56**。次回リリースは57以上
