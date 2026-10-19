# MLXBar v2.10.0 テスト計画・実施記録

**版数**: app 2.10.0 / build 55
**対象**: VLM誤起動ガード＋明示エンジン選択 / oMLXセットアップ / autoConnect / jobs.cancel頑健化
**作成日**: 2026-10-19
**実施環境**: Apple Silicon Mac / Python 3.13 (`Coordinator/.venv`) / Swift 6 toolchain

---

## 1. 方針

- 新機能は単体試験を新設テストファイルに分離して網羅する。
  - `Tests/test_v2_10_engine_detection.py`（機能1: 判別・ガード・明示選択。41件）
  - `Tests/test_v2_10_omlx_setup.py`（機能2: oMLXセットアップ。26件）
- 回帰は既存全件＋新設を合わせて `pytest -q ../Tests -p no:randomly` で1発確認。
- GUIは `swift build` による型整合で担保し、操作面は実機確認（§5）。

## 2. 機能1: VLM判別・エンジンガード（41件）

| 分類 | 検証内容 |
|---|---|
| classifier | VISION_ONLY_CONFIG の例（qwen2_vl / llava / internvl / mllama / paligemma / molmo / pixtral / minicpm-v / smolvlm / deepseek_vl / aria / ovis 等）が `engine=mlx-vlm` に分類される |
| classifier | テキスト系（qwen / smollm2 / laguna 以外の通常例）が誤って VLM 判定されない |
| classifier | laguna（mlx-vlm native テキスト系）が `mlx-vlm` に分類される |
| classifier | `detect_engine()` 再導出が classify と一致する |
| 保守 | `MLX_VLM_TEXT_MODEL_TYPES` / `ARCH_VISION_*` マーカーセットとテストの同期検証 |
| mlx-lmガード | 自動（allow_vlm=False）でVLM証拠なら `MODEL_INCOMPATIBLE`（アダプタ直呼び＋`create_app`経由の分類） |
| mlx-lmガード | 明示（allow_vlm=True）ではガードを通過し mlx-lm として実行を試みる |
| ガード | ワーカー未インストール時 `RUNTIME_NOT_INSTALLED` 分類 |
| supervisor | auto チェーン: 再導出→差し替え→対称フォールバック（1回）|
| supervisor | explicit チェーン: 名称優先・再導出なし・フォールバックなし・`allow_vlm=explicit` |
| pool | `_load_replica` 経由の自動ロード成功後 `_refresh_catalog_classification` が `Database.update_model_classification` を呼ぶ（自己修復） |
| pool | 明示ロードではカタログ再分類しない |
| db | `update_model_classification` の書込み内容（format/engine/modalities/confidence 0.7封印/reason） |
| metad | `CATALOG_CLASSIFIER_VERSION == "3"` |

## 3. 機能2: oMLXセットアップ（26件）

| 分類 | 検証内容 |
|---|---|
| requirements | `probe_cli` は15秒キャッシュ。`requirements(cli)` のキー構造 / CLI version 反映 |
| requirements | brew 無し（`no_brew()` patch）時に `brew.found=False`、実Homebrew不要に `_run_command` をdummy置換 |
| install | 「brew 無し」では `OMLX_BREW_NOT_FOUND`（409)。実Homebrew不要に `_run_command` をdummy置換 |
| install | 成功は共有 `_action_lock` の内側で完走、ジョブは必ずterminal終了 |
| start/stop | cli 不在で `OMLX_NOT_INSTALLED` |
| start | `omlx start` 成功、verify=True で `serverReachable=True`; verify は job枠内で実施 |
| start | verify が規定窓内で未到達のときも成功終了・`serverReachable=False` は嘘がない |
| stop | `omlx stop` 成功、stop後の到達が残る場合は `serverStillReachable` に真正 |
| 並行 | 同一アクションの重複投入は既存ジョブ返却、異アクションは直列化; `omlx_setup_job` 応答は state idle 差を伴わない。`test_actions_never_overlap`: async tracked 化済み（旧版は `_run_command` をawaitせず非実serial検証だったので書き直し）。probeはロック外 |
| jobs.cancel | `POST /omlx/setup/jobs/{id}/cancel` は kind が `omlx_*` 以外は404。running / task 未飛来でも terminal になる（jobs.py fallback） |

## 4. 全体回帰

| 項目 | 結果 |
|---|---|
| `pytest -q ../Tests -p no:randomly`（新設込み全件） | **721 passed** (30.2s) |
| 追加テスト詳細 | engine_detection 41 / omlx_setup 26 |
| 主な既存テスト改変 | FakeWorker の `load(model, engine, explicit=None)` 署名対応 6ファイル（`test_model_pool.py` 等） |
| 実バグ①検証 | `Tests/test_worker_server.py`: mlx_vlm adapter が `allow_vlm` 受け、serverが位置引数で渡せて通過 |
| `.strings` | `plutil -lint` OK。v2.10.0 追加キー20件の重複なし（既存7件の重複は v2.9.0 由来で今回の変更外 — 記録済） |

## 5. 実機確認（リリース後追記）

- build-release.sh による DMG 生成・`hdiutil verify`・codesign verify ベース
- `/Applications/MLXBar.app` 入替・サーバー起動・GUI から oMLX / 明示エンジン操作 → 結果をこの節に追記

## 6. 既知の不具合・制限

- 明示 engine 選択はロード時にのみ有効で、カタログの engine 列は auto ロード後にしか修復されない（仕様）。
- 誤って v2.9.0 として DMG 配布した実体は app plist が `2.8.1 / build 54`（v2.9.0 リリース時の packaging 更新漏れ）。本版で 2.10.0 / 55 に是正し、サイト側 `softwareVersion` も 2.10.0 に更新。
