# MLXBar v2.9.0 テスト計画

**版数**: app 2.9.0 / build 55  
**対象**: oMLX統合 / モデル単位ルーティング / 統合メモリ管理  
**作成日**: 2026-10-18

---

## 1. テスト方針

- **モックoMLXサーバー**を使用したAPI・障害系の自動試験
- **実際のoMLXサーバーとモデル**を使用した統合試験（実機）
- 既存機能の回帰テスト（v2.8.1テスト全件継続通過）

---

## 2. 自動テスト（モックoMLXサーバー）

### 2.1 設定・バリデーション

| ID | テストケース | 期待結果 |
|---|---|---|
| CFG-01 | oMLX設定の既定値確認 | `enabled: false`, `serverUrl: "http://127.0.0.1:8000"`, `apiKey: null`, `autoConnect: false`, `modelRouting: {}` |
| CFG-02 | 有効な設定で更新成功 | `enabled: true`, `serverUrl: "http://192.168.1.100:8000"`, `apiKey: "key"`, `autoConnect: true`, `modelRouting: {"model-1": true}` が保存される |
| CFG-03 | 不正URLで422 | `serverUrl: "ftp://example.com"` → `ValueError: models.omlx.serverUrl must be an http(s) URL` |
| CFG-04 | modelRoutingキー空文字で422 | `modelRouting: {"": true}` → `ValueError: keys must be non-empty strings` |
| CFG-05 | modelRouting値非booleanで422 | `modelRouting: {"model-1": "yes"}` → `ValueError: modelRouting[model-1] must be boolean` |
| CFG-06 | apiKey非文字列で422 | `apiKey: 123` → `ValueError: models.omlx.apiKey must be a string or null` |
| CFG-07 | 設定エクスポートにoMLX含む | `GET /settings/export` に `models.omlx` が含まれる（apiKeyは除外） |
| CFG-08 | 設定インポートでoMLX適用 | 有効なexport JSONでimport → 設定反映、不正項目はskip理由付き |

### 2.2 oMLX Adapter（モックサーバー）

| ID | テストケース | 期待結果 |
|---|---|---|
| ADPT-01 | 接続テスト成功（admin API） | `/admin/api/health` 200 → `connected: true`, `version` 取得 |
| ADPT-02 | 接続テスト成功（フォールバック） | `/admin/api/health` 404 → `/v1/models` 200 → `connected: true`, `version: null` |
| ADPT-03 | 接続テスト失敗（接続不可） | 接続エラー → `connected: false`, `error` に詳細 |
| ADPT-04 | 接続テスト失敗（タイムアウト） | 10秒タイムアウト → `connected: false`, `error: "Connection timeout"` |
| ADPT-05 | モデル一覧取得（admin API） | `/admin/api/models` 200 → `OMLXModelInfo` リスト（loaded, memory_bytes, mtp_enabled, ple_offload 含む） |
| ADPT-06 | モデル一覧取得（フォールバック） | admin API失敗 → `/v1/models` 200 → 基本情報のみ（loaded=false） |
| ADPT-07 | モデルロード成功 | `POST /v1/models/{id}/load` 200 → 結果返却 |
| ADPT-08 | モデルロード失敗（4xx） | 400/404 → `MLXBarError("OMLX_LOAD_FAILED", ...)` |
| ADPT-09 | モデルロード失敗（5xx） | 500/503 → `MLXBarError(..., retryable=True)` |
| ADPT-10 | モデルロードタイムアウト | 300秒超過 → `MLXBarError("OMLX_LOAD_TIMEOUT", retryable=True)` |
| ADPT-11 | モデルアンロード成功 | `POST /v1/models/{id}/unload` 200 → 結果返却 |
| ADPT-12 | ストリーミング生成 | `/v1/chat/completions` SSE → `delta`/`tool_call_delta`/`completed`/`usage` イベント正しくyield |
| ADPT-13 | 生成エラー応答 | 4xx/5xx → `error` イベントyield（retryable判定付き） |
| ADPT-14 | 生成タイムアウト | 読み取りタイムアウト → `error` イベント（retryable=True） |
| ADPT-15 | キャンセル（ノーオペ） | `cancel_generation()` → `True` 返却（実装上は即時返却） |
| ADPT-16 | メモリ取得 | `get_model_memory()` / `get_total_memory()` → 正しい値返却 |
| ADPT-17 | キャッシュ動作 | `get_status(force_refresh=False)` 5秒以内ならキャッシュ返却 |

### 2.3 OMLXProvider

| ID | テストケース | 期待結果 |
|---|---|---|
| PROV-01 | connect() → adapter.test_connection() | 同等結果返却 |
| PROV-02 | status() → adapter.get_status() | 辞書形式で正しく整形（models配列含む） |
| PROV-03 | list_models() → adapter.list_models() | 辞書リスト返却（providerKey含む） |
| PROV-04 | load() 成功 | ルーティング確認 → adapter.load_model() → loaded_info返却（engine: "omlx", provider: "omlx"） |
| PROV-05 | load() ルーティング無しで失敗 | `_is_model_routed_to_omlx=False` → `MLXBarError("MODEL_INCOMPATIBLE")` |
| PROV-06 | load() engine mismatchで失敗 | `engine="mlx-lm"` 指定 → `MLXBarError("ENGINE_MISMATCH")` |
| PROV-07 | unload() 成功 | adapter.unload_model() → `_loaded_models` から削除 |
| PROV-08 | generate() 成功 | 未ロードならエラー → adapter.generate() ストリーム中継 |
| PROV-09 | cancel() | adapter.cancel_generation() → 結果返却 |
| PROV-10 | get_memory_usage() | adapter.get_total_memory() + 各モデルget_model_memory() 返却 |
| PROV-11 | close() | バックグラウンドタスク停止 + adapter.close() |
| PROV-12 | start_status_updates() | タスク作成・重複防止 |
| PROV-13 | _status_update_loop() | 5秒間隔で_update_cached_status() 実行、エラー無視 |

### 2.4 ModelPoolSupervisor統合

| ID | テストケース | 期待結果 |
|---|---|---|
| POOL-01 | _is_omlx_model() 判定 | `modelRouting: {"model-a": true}` で model-a はTrue、model-b はFalse |
| POOL-02 | _omlx_enabled() 判定 | `enabled: true` でTrue、falseでFalse |
| POOL-03 | oMLXモデルのロード | `_is_omlx_model=True` → `_omlx_provider.load()` 呼出、Worker作成せず |
| POOL-04 | oMLXモデルのアンロード | `_is_omlx_model=True` → `_omlx_provider.unload()` 呼出 |
| POOL-05 | oMLXモデルの生成 | `_is_omlx_model=True` → `_omlx_provider.generate()` ストリーム中継 |
| POOL-06 | 非oMLXモデルは既存経路 | `_is_omlx_model=False` → 既存Worker経路（変更なし） |
| POOL-07 | _resident_charge() にoMLX含む | oMLX接続時、キャッシュステータスの `totalMemoryBytes` が加算される |
| POOL-08 | _admit() でoMLX分を予算から差引 | oMLX使用中に新規ロード → 予算からoMLXメモリ差引後の値で判定 |
| POOL-09 | status() にoMLXモデル含む | `loadedModels` に `engine: "omlx"`, `managedExternally: true`, `mtpEnabled`, `pleOffload` 含む |
| POOL-10 | oMLXモデルはresident: false | `resident: false`（MLXBar常駐スロットを消費しない） |
| POOL-11 | shutdown() でprovider close | `_omlx_provider.close()` 呼出 |

### 2.5 Management API

| ID | テストケース | 期待結果 |
|---|---|---|
| API-01 | POST /omlx/connect | プロバイダ存在時 → connect() 結果返却、未初期化時 → 503 |
| API-02 | GET /omlx/status | プロバイダ存在時 → status() 結果返却、未初期化時 → 503 |
| API-03 | GET /omlx/models | プロバイダ存在時 → list_models() 結果返却、未初期化時 → 503 |
| API-04 | POST /omlx/models/{id}/load | ルーティング確認 → load() → 結果返却、ルーティング無し → 409 |
| API-05 | POST /omlx/models/{id}/unload | ルーティング確認 → unload() → 結果返却、ルーティング無し → 409 |
| API-06 | GET /settings/omlx-token | トークン存在時 → `{"token": "...", "configured": true}`、無時 → `{"token": "", "configured": false}` |
| API-07 | PUT /settings/omlx-token | 正常設定 → 保存・返却、不正値 → 422 |

### 2.6 CLI

| ID | テストケース | 期待結果 |
|---|---|---|
| CLI-01 | `omlx connect` | POST /omlx/connect 呼出・結果表示 |
| CLI-02 | `omlx status` | GET /omlx/status 呼出・結果表示 |
| CLI-03 | `omlx models` | GET /omlx/models 呼出・結果表示 |
| CLI-04 | `omlx load <id>` | POST /omlx/models/{id}/load 呼出・結果表示 |
| CLI-05 | `omlx unload <id> [--force]` | POST /omlx/models/{id}/unload[?force=true] 呼出・結果表示 |
| CLI-06 | `omlx set-enabled true|false` | PUT /settings に `models.omlx.enabled` 送信 |
| CLI-07 | `omlx set-url <url>` | PUT /settings に `models.omlx.serverUrl` 送信 |
| CLI-08 | `omlx set-routing <id> true|false` | PUT /settings に `models.omlx.modelRouting.{id}` 送信 |
| CLI-09 | `secrets get-omlx-token` | GET /settings/omlx-token 呼出・表示 |
| CLI-10 | `secrets set-omlx-token <token>` | PUT /settings/omlx-token 呼出・結果表示 |

---

## 3. 統合テスト（実機・実oMLXサーバー）

### 3.1 環境要件

- Mac Studio M3 Ultra / 96GB ユニファイドメモリ / macOS
- oMLXサーバー v0.7.0以上 稼働中（`http://127.0.0.1:8000`）
- テスト用モデル: `Jundot/Qwen3.8-Flash-Next-oQ4e-mtp` 等、oMLX最適化済みモデル

### 3.2 テストシナリオ

| ID | シナリオ | 手順 | 期待結果 |
|---|---|---|---|
| INT-01 | 基本接続・モデル一覧 | 1. oMLX起動 2. MLXBar起動 3. 設定→oMLXタブ→接続テスト 4. モデル一覧表示 | 接続成功、バージョン表示、モデル一覧取得（10件以上） |
| INT-02 | モデルルーティングON/OFF | 1. モデルAをON、モデルBをOFF 2. 設定保存 3. 再起動 4. 設定確認 | 設定永続化、再起動後もON/OFF維持 |
| INT-03 | oMLXモデルロード | 1. モデルAをON 2. MLXBarからロード 3. ステータス確認 | oMLX側でロード完了、MLXBarステータスにLoaded表示、メモリ・MTP・PLE表示 |
| INT-04 | oMLXモデル推論（非ストリーム） | 1. ロード済みモデルAでチャット 2. 応答確認 | 正常応答、生成速度表示、usage取得 |
| INT-05 | oMLXモデル推論（ストリーム） | 1. ストリーミングONでチャット 2. トークン逐次表示確認 | SSEストリーミング正常、トークン逐次到達 |
| INT-06 | Tool Calling | 1. tools指定でリクエスト 2. tool_call_delta → completed確認 | tool call正常中継、finish_reason: tool_calls |
| INT-06 | 生成キャンセル | 1. 長い生成開始 2. 中断ボタン/キャンセルAPI 3. 停止確認 | 生成停止、リソース解放、次のリクエスト正常 |
| INT-07 | 非oMLXモデルとの共存 | 1. モデルAをoMLX、モデルBをmlx-lmでロード 2. 両方で推論 | 両ランタイム独立動作、干渉なし |
| INT-08 | メモリ予算統合（96GB環境） | 1. oMLXで大型モデル（約60GB）ロード 2. mlx-lmで中型モデル（約20GB）ロード試行 | 合計80GBで予算内なら成功、超過ならMEMORY_BUDGET_EXCEEDEDで拒否 |
| INT-09 | oMLX停止時の動作 | 1. oMLXモデルロード中にoMLXサーバー停止 2. MLXBar操作継続 | MLXBar本体は継続、oMLXモデルのみエラー、他ランタイム正常 |
| INT-10 | oMLX再接続 | 1. oMLX停止→再起動 2. 接続テスト実行 | 再接続成功、モデル状態再取得 |
| INT-11 | アンロード・再ロード | 1. oMLXモデルロード 2. アンロード 3. 再ロード | 正常にアンロード・再ロード、oMLX側インスタンス管理正常 |
| INT-12 | 設定エクスポート/インポート | 1. oMLX設定込みでエクスポート 2. 別環境でインポート | oMLX設定（apiKey除く）正常移行、apiKeyは手動再入力 |
| INT-13 | メニューバー表示 | 1. oMLXモデルロード中 2. メニューバー開く | residentModelsにoMLXモデル表示、engine: oMLX、メモリ・生成速度・MTP/PLE表示 |

---

## 4. 回帰テスト（既存機能）

| 領域 | 確認項目 |
|---|---|
| mlx-lm/mlx-vlm | ロード・アンロード・推論・ストリーミング・Tool Calling・キャッシュ再利用・Paged KV・KV量子化 |
| LM Studio | 接続・ロード・推論・モデル切替 |
| モデルプール | 常駐/非常駐・レプリカ・同時生成・TTL・アイドル延長 |
| メモリ管理 | 予算計算・プレッシャー検知・自動退避・ヘッドルーム |
| 設定 | プリセット・エクスポート/インポート・セットアップウィザード |
| ランタイム管理 | チェック・ステージ・アクティベート・ロールバック |
| RAG | コレクション・ドキュメント・クエリ・埋め込み |
| Adaptive Memory | 有効化・プランニング・フォールバック |
| CLI | 全サブコマンド動作・JSON出力・エラー処理 |
| Swift UI | 全設定タブ表示・日英切替・スクロール・状態更新 |

---

## 5. パフォーマンス・ストレステスト

| ID | テスト | 条件 | 合格基準 |
|---|---|---|---|
| PERF-01 | 同時接続負荷 | 10クライアント×100リクエスト | エラー率<1%、p99レイテンシ<5秒 |
| PERF-02 | 長時間稼働 | 24時間連続稼働・定期推論 | メモリリークなし、クラッシュなし |
| PERF-03 | 大型モデル切替 | 27B級モデルをoMLX/nativeで交互切替 | 切替時間<3分、メモリ正常解放 |
| PERF-04 | メモリ逼迫シミュレート | 物理メモリ90%使用状態でロード試行 | 適切に拒否/退避、システムフリーズなし |

---

## 6. 完了条件

- [ ] 自動テスト（モック）全件パス
- [ ] 統合テスト（実機）主要シナリオ全件パス
- [ ] 回帰テスト（v2.8.1既存機能）全件パス
- [ ] パフォーマンステスト合格基準クリア
- [ ] ドキュメント（DESIGN/RELEASE_NOTES/TEST_PLAN）完成
- [ ] Swift build成功・警告なし
- [ ] DMG作成・署名・検証成功
- [ ] GitHub Release作成（DMG + .sha256）
- [ ] Webサイト更新（studio-rizi、4言語）

---

## 7. テスト実行コマンド

```bash
# Python自動テスト
cd /Users/yuki/適当フォルダ/mlx-bar
python3 -m pytest Tests/ -v

# 特定テストファイル
python3 -m pytest Tests/test_cli.py -v -k "omlx"
python3 -m pytest Tests/test_model_pool.py -v

# Swiftビルド
swift build

# リリースビルド・検証
VERSION=2.9.0 sh scripts/build-release.sh
VERSION=2.9.0 sh scripts/verify-release.sh
```