# DESIGN v2.10.0 — VLM誤起動ガード＋明示エンジン選択 / oMLXセットアップ + autoConnect

- 版数: app 2.10.0 / build 55。Coordinator `__version__` 2.10.0。`schemaVersion` は 1 のまま（変更なし）。
  - **訂正**: v2.9.0 としてDMG配布した実体の `Packaging/Info.plist` は `2.8.1 / build 54` のまま
    出荷されていた（`TEST_PLAN_v2.9.0.md` 冒頭の build 55 記載は誤り、実配送は54）。
    このため v2.10.0 は CFBundleVersion を 55 に進め、同一app IDでのビルド番号の単調性を回復する。
- 方針: 明示的なユーザー選択を常に最優先にし、自動判定は補助に徹する。安全設計・互換性・
  クラッシュ/メモリ安全性・日英完全対応UIを守る。既存API形状・既定値・設定スキーマは不変。

---

## 1. 背景

### 1.1 ビジョンモデルの mlx-lm 誤起動

v2.9.0 のカタログ判別は `config.json` の構造的証拠（`vision_config` / `vision_tower` など
5キー、processor ファイル・processor_class）中心で、次のギャップがあった。

- アーキ名で判るVLM系（llava / internvl / idefics / mllama / paligemma / molmo / pixtral /
  minicpm(v) / smolvlm / deepseek_vl / aria / ovis など）は構造的証拠が欠けると mlx-lm に誤分類。
- mlx-vlm がネイティブ実装を持つテキストのみのアーキ（laguna）は vision 証拠ゼロなので
  mlx-lm に回り、mlx-lm では読めない。

誤分類のまま auto ロードすると mlx-lm ワーカーが vision テンソルを解釈できず失敗する。

### 1.2 oMLXの導入導線

oMLX は外部サーバー（https://github.com/jundot/omlx、Homebrew tap `jundot/omlx/omlx`）。
v2.9.0 は「接続先URL・APIキーを設定して接続」しかできず、未インストール/停止のユーザーは
ターミナルで brew を叩く必要があった。

### 1.3 autoConnect 未実装

`models.omlx.autoConnect` は v2.9.0 に設定値として存在したが、読み取り箇所がなく、
Coordinator 再起動後の接続はユーザー操作前提だった。

---

## 2. カタログ分類 v3（`CATALOG_CLASSIFIER_VERSION = "3"`）

`Coordinator/mlxbar/catalog/classifier.py`:

- `VISION_KEYS` を拡張（`vision_config` / `vision_tower` / `vision_start_token_id` /
  `image_token_id` / `image_token_index` / `image_seq_length` / `video_token_id` /
  `mm_projector_type` / `mm_vision_select_layer` など）。
- `ARCH_VISION_TOKENS`（トークン単位）＋ `ARCH_VISION_MARKERS`（部分一致）で
  architectures / model_type 文字列を判定。「qwen2_vl」は `vl` トークンで一致し、
  「qwen2」「smollm2」等のテキスト系は誤マッチしないよう分割後の集合評価で落とす。
- `MLX_VLM_TEXT_MODEL_TYPES = {"laguna"}`: mlx-vlm ネイティブのテキスト系は直接 mlx-vlm。
- `CATALOG_CLASSIFIER_VERSION` を "3" に上げ、起動時に旧版メタデータが残っていれば
  再スキャン（`main.py` の既存再スキャン条件にバージョン比較があった）。

---

## 3. 明示エンジン選択とフォールバック規則

**決定: 明示選択を常に優先。自動判定は「補助」のみ。**

### 3.1 supervisor（`SingleWorkerSupervisor.load`）

```
load(model, engine=None, explicit=None)
```

- `explicit` は「呼び出し側が名指した」（管理APIのbody engine / CLI `--engine` /
  reload経由のレプリカ再現など）ことを記録。未指定なら `engine != "auto"` で補完する。
- `engine == "auto"` は None と同義に正規化（v2.9.0 では pool 経由で正規化されず
  「利用可能なエンジンがありません」で拒否されるバグ＝実バグ修正②）。
- **明示時**: engine再導出なし、フォールバックなし、カタログ再分類なし。
  ワーカーへは `allow_vlm=True` を渡し、ユーザーの選択を最後まで実行する
  （失敗はそのままユーザーに返る）。
- **自動時**: `detect_engine(Path)` でロード時再導出し、カタログ行と異なれば差し替え。
  失敗時に旧 mlx-lm→mlx-vlm レスキューを双方向化（mlx-lm⇄mlx-vlm）を1回のみ。
- mlx-lm アダプタ側ガード（`looks_like_vlm` でVLM証拠なら `MODEL_INCOMPATIBLE`）
  は `allow_vlm=False`（自動）のときだけ有効。実バグ修正①:
  server が両アダプタへ `allow_vlm` を渡す改修に対し
  `Workers/mlx_vlm_worker/adapter.py::load` が未対応だったのを修正（v2.9.0 では
  mlx-vlm ワーカーへの load が必ず TypeError で実機能不全だった）。

### 3.2 pool（`ModelPoolSupervisor`）

- `_load_replica` 経由の自動ロードが成功した後、カタログ行を
  `_refresh_catalog_classification()`（v2.10.0 新設）で自己修復:
  `format` / `modalities` は現classifier再現、`engine` はロード結果、
  confidence は 0.7 封印、reason にロード時再検出を記録。
  失敗はログのみ。明示ロードでは決して呼ばない（一回限りの上書きをカタログに反映しない）。

### 3.3 GUI（`ModelCatalogView`）

- モデル一覧に「エンジン指定」メニュー（mlx-lm / mlx-vlm）を追加。
  ロード時に `engine` を指定する一回限りの選択で、永続的な per-model 設定キーは作らない
  （次のAPIオートロードはカタログ＋ロード時再導出に戻る — 仕様）。
- 文字列は LS() キー4本（ja=ソース、en翻訳付き）。

---

## 4. oMLXセットアップ（`omlx_setup.py`）

### 4.1 サービス

- `probe_cli()`: `omlx` CLI（PATH / `~/.omlx/bin/omlx`）検出＋ `--version` 取得。15秒キャッシュ。
- `brew_status()`: `shutil.which("brew")` ＋ `/opt/homebrew/bin/brew` フォールバック。
- `requirements(cli)`: `cli{found,path,version}` / `brew{found,path,installCommand}` /
  `installed` / `releasesUrl` / `installCommand` を1辞書で返す。GUIの描画入力。
- `install(update)`: `brew install jundot/omlx/omlx` を子プロセスグループ隔離
  （SIGTERM→SIGKILL、ハートビート進捗）でジョブ化。成功後 CLI を強制再probe。
- `start_server(update, verify)` / `stop_server(update, verify)`:
  `omlx start` / `omlx stop`。start 後は注入された `verify()`
  （`AppState.omlx_connect_probe` = Provider.connect() 戻り値の `connected`）で
  設定URLへの到達を最大2分待つ。stop 後もまだ答えるかどうかを1回確認。
  到達性は嘘をつかず `serverReachable` / `serverStillReachable` に報告
  （stop の拒否は非致死的）。
- 全アクションは `asyncio.Lock` で直列（probe のみロック外）。
  すべて `jobs.create("omlx_{install,start,stop}", ...)` のジョブに入り、
  進捗・202応答・`/jobs/{id}` ポーリングはランタイム更新 jobs と同一機構。
  キャンセルは `POST /omlx/setup/jobs/{id}/cancel`（kind ホワイトリスト検査付き）。

### 4.2 管理API

```
GET  /api/v1/omlx/setup/requirements
POST /api/v1/omlx/setup/install   → 202
POST /api/v1/omlx/setup/start     → 202
POST /api/v1/omlx/setup/stop      → 202
POST /api/v1/omlx/setup/jobs/{id}/cancel
```

- 同一アクションの queued/running ジョブは再送しても重複しない（`state.omlx_setup_jobs`）。
- セットアップ系は設定を書かない。接続設定は `models.omlx`（settings.py正系）＋
  `control/omlx-token` に属する。

### 4.3 CLI

`mlxbarctl omlx install|start|stop [--wait]`（v2.9.0 の omlx サブコマンドに追加）。

### 4.4 GUI（`RuntimeManagerView` の oMLXカード）

- requirements 表示（インストール済み/未、CLI version、brew不備時はリリースページリンク）、
  install / start / stop ボタン、ジョブカード（進捗・メッセージ・キャンセル）。
- メッセージはサーバーから日本語原文で受信し、英語UIでは既知パターンを置換表示。
- ジョブ終了後は requirements / status を再取得。

---

## 5. /omlx/status・connect 正形パネル + GUI デコーダ

### 5.1 Server canonical payload（`omlx_adapter._model_payload`）

```
{
  "connected": bool, "version": str|null, "error": str|null,
  "serverUrl": str, "totalMemoryBytes": int,
  "models": [{ "id", "name", "loaded", "memoryBytes", "contextLength",
               "mtpEnabled", "pleOffload", "engine", "providerKey",
               "available": true }, ...]
}
```

- `POST /omlx/connect` は `{connected, version, error, serverUrl}` を返す。
  `success` キーは存在しない（v2.9.0 GUI は `success` を読んでいたため
  接続テストが常に失敗表示になる実バグ③）。

### 5.2 GUI

- `OMLXModel`: canonical decode（`memoryBytes`→GB換算、旧 `memoryGB` フォールバック、
  `pleOffload` は旧 `pleOffloadEnabled` フォールバック、`available` 追加）。
- `refreshOMLXStatus`: `models.omlx.modelRouting` を正系にルーティング合成。
  `available=false` のモデルは routing が true でも useOMLX=false。
- `setOMLXEnabled` / `setOMLXServerUrl` / `setOMLXModelRouting` は
  `PUT /settings` に `{"models":{"omlx":{...}}}` を送る。PUT は deep-merge なので
  routing は単一キーのPATCHのみでよい（v2.9.0 は全 routing 再送で
  並行編集を消していた）。
- `testOMLXConnection` は `connected` を読む。
- `RuntimeJobInfo.operationName` に `omlx_install` / `omlx_start` / `omlx_stop`
  を追加し、セットアップjobの見出しを固有化。

---

## 6. autoConnect

- `main.serve()` が管理API起動後に作るバックグラウンドタスク `_omlx_auto_connect_task`:
  `models.omlx.{enabled,autoConnect}` かつ provider があり、かつシャットダウン前のとき発動。
  `state.workers.connect_omlx()`（v2.10.0 新設の公開中継。`provider.connect()` の透過）を実行、
  失敗時は20秒間隔で最大6回試行（合わせて約2分。双方が launchd で同時起動する場合の猶予）。
  以後は断念し、status 更新ループ／手動 `/omlx/connect` があとを担う。
- 例外・中断はログのみ。Coordinator 起動は決して妨げない。

---

## 7. jobs.cancel の頑健化

v2.9.0 までの cancel は「running 中にワーカーtaskが飛来済み」の前提で、queued や
「running だが task 生成前」のフェーズで呼ばれると DB に terminal が書かれない
ゴーストjobを残した。`cancel` は常時 DB terminal 書込み ＋ queue event ＋
tasks/queues の pop をfallbackとして行う（Setup job cancel と
start の到達待ち時間中の cancel 等はこれに依存）。

---

## 8. 後方互換性・安全性

- REST/CLI/API形状・既定値・設定スキーマは不変。`models.omlx` は v2.9.0 から継続。
- 自動フォールバック・自動再分類・autoConnect・インストールジョブはすべて、
  明示操作（GUIボタン / CLI / API呼び出し）からのみ走る。
- oMLX未インストール環境では認知・表示のみで動作に影響がない
  （brew無し環境はリリースページ案内に誘導）。
- テスト: Python 721件 全通過。追加は
  `Tests/test_v2_10_engine_detection.py`（41件）、`Tests/test_v2_10_omlx_setup.py`（26件）。

## 9. 既知の制限

- 明示 engine 選択はロード時の一回限り。カタログの engine 列は書き換えない。
- `omlx start` はその場で `omlx` CLI を1回実行するのみ。マシン再起動後の
  自動再起動は oMLX 側（`brew services` 等）の仕様に依存する。
- GUIのジョブ監視は共通上限（30分）で打ち切る。
