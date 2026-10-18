# DESIGN v2.9.0 — oMLX統合 / モデル単位ルーティング / 統合メモリ管理

- 版数: app 2.9.0 / build 55。Coordinator `__version__` 2.9.0。`schemaVersion` は 1 のまま（変更なし）。
- 方針: 既存ランタイム（mlx-lm、mlx-vlm、LM Studio）を維持しつつ、oMLXを第4の推論バックエンドとして追加。モデルごとに使用ランタイムを指定可能。安全設計・互換性・メモリ安全性・日英完全対応UIを守る。

---

## 1. 背景と目的

oMLX（https://github.com/jundot/omlx）は、PLE SSD Offload・Lightning MTP・TurboQuant KV・ANE Prefill・MoE Expert Offloadなど、Apple Siliconに特化した高速化機能を持つ推論サーバーである。MLXBarはこれまでmlx-lm、mlx-vlm、LM Studioの3つのランタイムをサポートしてきたが、oMLX固有の最適化をMLXBarから利用できるようにすることが目的。

**重要な設計方針：**

- 全モデルをoMLXへ移行するのではなく、**ユーザーが明示的に指定したモデルのみ、oMLXへルーティング**する
- oMLXを選択していないモデルは、これまでどおり既存のランタイムで動作
- oMLX側の推論処理をMLXBarへ移植せず、既存のoMLX API（Admin API + OpenAI互換API）を通じて制御
- oMLXで使用しているメモリも、MLXBar全体のメモリ使用量・メモリ予算に必ず含める

---

## 2. アーキテクチャ

```
MLXBar
│
├── GUI / CLI / OpenAI互換API / Anthropic互換API
│
├── 統合モデル管理・メモリ管理
│   │
│   ├── mlx-lm
│   ├── mlx-vlm
│   ├── LM Studio Provider
│   └── oMLX Provider（新規）
│       │
│       └── oMLX Server (External Process)
│           ├── PLE SSD Offload
│           ├── Lightning MTP
│           ├── KV Cache (TurboQuant, Prefix/SSD Cache)
│           └── モデル推論
│
└── 統合ステータス表示
```

### 2.1 新規コンポーネント

| コンポーネント | ファイル | 役割 |
|---|---|---|
| oMLX Settings | `settings.py` | `models.omlx` セクション追加 |
| oMLX Adapter | `workers/omlx_adapter.py` | oMLX Admin API + 推論APIとの通信 |
| oMLX Provider | `workers/omlx_adapter.py` | Worker Supervisor互換の高レベルインターフェース |
| Model Pool統合 | `workers/model_pool.py` | oMLXルーティング・メモリ統合・生成ルーティング |
| Management API | `api/management.py` | oMLX接続・モデル操作エンドポイント |
| CLI Commands | `cli.py` | `mlxbarctl omlx` / `secrets` コマンド |
| Swift UI | `MLXBarSettingsView.swift` / `MenuBarViewModel.swift` | oMLX設定タブ・メニューバー表示 |

---

## 3. 設定スキーマ（`settings.py`）

```json
"models": {
  "omlx": {
    "enabled": false,                    // oMLX連携の有効/無効
    "serverUrl": "http://127.0.0.1:8000", // oMLXサーバーURL
    "apiKey": null,                       // APIキー（別ファイル管理）
    "autoConnect": false,                 // 起動時自動接続
    "modelRouting": {}                    // model_id -> use_omlx (bool)
  }
}
```

**検証ルール（`_validate`）：**
- `enabled` / `autoConnect` は boolean
- `serverUrl` は http:// または https:// で始まる文字列
- `apiKey` は文字列または null
- `modelRouting` はオブジェクト、キーは非空文字列、値は boolean
- oMLXトークンは `~/Library/Application Support/MLXBar/control/omlx-token` に別管理（設定JSONに含めない）

---

## 4. oMLX Adapter / Provider

### 4.1 OMLXAdapter（低レベルHTTPクライアント）

| メソッド | 説明 |
|---|---|
| `test_connection()` | `/admin/api/health` → `/v1/models` フォールバックで接続確認 |
| `get_status()` | キャッシュ付きステータス取得（5秒TTL） |
| `list_models()` | `/admin/api/models` → `/v1/models` フォールバック |
| `load_model(id)` | `POST /v1/models/{id}/load` |
| `unload_model(id)` | `POST /v1/models/{id}/unload` |
| `generate()` | OpenAI互換 `/v1/chat/completions` ストリーミング |
| `cancel_generation()` | クライアント側ストリームクローズ（oMLXに専用エンドポイントなし） |
| `get_model_memory()` / `get_total_memory()` | メモリ使用量取得 |

**対応機能：**
- OpenAI互換Chat Completions / SSEストリーミング
- Tool Calling / Tool Choice
- 生成キャンセル（クライアント側）
- エラー・タイムアウト伝達
- トークン使用量・生成速度取得

### 4.2 OMLXProvider（高レベルプロバイダ）

`WorkerSupervisor`と同等のインターフェース：
- `connect()` / `status()` / `list_models()`
- `load(model)` / `unload(model_id)`
- `generate(model_id, prompt, images, options, request_id)`
- `cancel(request_id)` / `get_memory_usage()`
- `close()` / バックグラウンドステータス更新タスク（5秒間隔）

---

## 5. ModelPoolSupervisor統合（`model_pool.py`）

### 5.1 ルーティング判定

```python
def _is_omlx_model(self, model_id: str) -> bool:
    routing = self.settings.data.get("models", {}).get("omlx", {}).get("modelRouting", {})
    return routing.get(model_id, False)
```

- `modelRouting` で指定されたモデルのみoMLXへ
- `engine == "omlx"` または `_is_omlx_model()` 真ならoMLXパスへ

### 5.2 ロード・アンロード

**ロード（`load`）：**
- oMLXモデルの場合：Workerプロセスを作らず、`_omlx_provider.load()` を呼出
- メモリ予約はoMLX側の実測を信頼せず、カタログ推定値で `_admit()` 実行
- `pin=True` は `session_pinned` 相当（oMLX側では常駐管理が別）

**アンロード（`unload_model` / `unload`）：**
- oMLXルーティングモデルは `_omlx_provider.unload()` へ委譲

### 5.3 生成ルーティング（`generate_for_model`）

```python
if self._omlx_enabled() and self._is_omlx_model(model_id):
    async for event in self._omlx_provider.generate(...):
        yield event
else:
    # 既存Worker経路
```

### 5.4 メモリ予算統合【重要】

```python
def _resident_charge(self) -> int:
    charge = sum(slot.reservation_bytes for slot in self._slots.values() if slot.state in _CHARGED_STATES)
    if self._omlx_enabled():
        charge += self._get_omlx_memory_usage_cached()  # oMLX分を加算
    return charge

async def _admit(self, model: dict, *, resident: bool) -> tuple[int, list[PoolSlot]]:
    budget = self._global_budget()
    if self._omlx_enabled():
        omlx_memory = await self._get_omlx_memory_usage()
        budget = max(0, budget - omlx_memory)  # oMLX使用分を予算から差引
```

- `_resident_charge()`：常駐メモリ総計にoMLX分を含める
- `_admit()`：新規ロード許可判定時にoMLX使用メモリを考慮
- 取得できない場合は「取得不可」として扱い、推測で確定値としない

### 5.5 ステータス表示統合（`status()`）

`loadedModels` にoMLXモデルを含める：
```python
{
    "id": "...", "name": "...", "engine": "omlx",
    "poolState": "ready", "memoryReservationBytes": ...,
    "activeLeases": 0, "keepLoaded": False,
    "managedExternally": True, "replicaCount": 1,
    "generationTokensPerSecond": 42.5,
    "mtpEnabled": true, "pleOffload": true,
    "resident": false
}
```

- `managedExternally: true` でoMLX管理を明示
- MTP / PLE Offload 状態はoMLXステータスから取得
- `resident: false`（oMLXモデルはMLXBarの常駐スロットを消費しない）

---

## 6. Management API（`api/management.py`）

| エンドポイント | メソッド | 説明 |
|---|---|---|
| `/omlx/connect` | POST | 接続テスト |
| `/omlx/status` | GET | 接続状態・バージョン・モデル一覧 |
| `/omlx/models` | GET | 利用可能モデル一覧 |
| `/omlx/models/{id}/load` | POST | モデルロード |
| `/omlx/models/{id}/unload` | POST | モデルアンロード |
| `/settings/omlx-token` | GET/PUT | APIキー取得・設定 |

---

## 7. CLI（`cli.py`）

```
mlxbarctl omlx connect           # 接続テスト
mlxbarctl omlx status            # ステータス表示
mlxbarctl omlx models            # モデル一覧
mlxbarctl omlx load <model_id>   # ロード
mlxbarctl omlx unload <id> [--force]  # アンロード
mlxbarctl omlx set-enabled <true|false>
mlxbarctl omlx set-url <url>
mlxbarctl omlx set-routing <model_id> <true|false>

mlxbarctl secrets get-omlx-token
mlxbarctl secrets set-omlx-token [token]
```

---

## 8. Swift UI実装

### 8.1 設定タブ（`MLXBarSettingsView.swift` → `OMLXSettingsView`）

**3セクション構成：**

1. **接続設定**
   - Toggle: oMLX連携を有効化
   - TextField: oMLXサーバーURL（初期値 `http://127.0.0.1:8000`）
   - SecureField: APIキー（表示/非表示切替）
   - Button: 接続テスト（スピナー付き）
   - 状態バッジ: 未接続/接続中/接続済み/エラー
   - oMLXバージョン表示

2. **モデルルーティング**
   - oMLXから取得したモデル一覧
   - 各行: Toggle「oMLXで実行する」、エンジンバッジ、ロード状態
   - ロード済み時: メモリ(GB)、生成速度(tok/s)、MTP、PLE Offload表示
   - Load/Unloadボタン
   - oMLX側で利用不可のモデルは無効化・警告表示

3. **状態**
   - 接続状態、バージョン、エラー詳細
   - ロード済みモデル一覧（詳細メトリクス付き）

### 8.2 メニューバー統合（`MenuBarViewModel.swift`）

- `ResidentModel` に oMLXモデル含む（`managedExternally: true`）
- `engine: "omlx"` でランタイム識別
- MTP / PLE Offload 状態表示
- 生成速度・メモリ使用量表示

### 8.3 ローカライゼーション

`en.lproj` / `ja.lproj` の `Localizable.strings` に全文言追加済み。

---

## 9. 後方互換性と安全性

| 項目 | 対応 |
|---|---|
| oMLX無効時の既存動作 | 完全維持（`enabled: false` 既定） |
| 既存モデルのランタイム選択 | 維持（oMLX未指定なら従来通り） |
| API仕様変更 | なし（既存エンドポイント不変） |
| oMLX停止時のMLXBar動作 | 正常継続（oMLXモデルのみ利用不可） |
| 再接続 | 接続テストボタン / 自動再取得（ステータスポーラー） |
| 二重ロード防止 | `_is_omlx_model()` + Worker側チェック |
| APIキー漏洩防止 | 別ファイル管理・ログ出力なし |
| 設定永続化 | `settings.json` に `modelRouting` 保存 |
| 生成中断・キャンセル安全処理 | 既存の `_aclose_generation` / `cancel` パス再利用 |

---

## 10. 受け入れ条件

- [x] oMLXを第4のランタイムとして選択可能
- [x] 「oMLX」専用設定タブが存在する
- [x] モデル単位でoMLX利用をON/OFF可能
- [x] 選択したモデルだけがoMLXに送信される
- [x] oMLXからモデル一覧・状態を取得可能
- [x] MLXBarからoMLXモデルをロード・アンロード可能
- [x] 通常チャットとSSEストリーミングが動作する
- [x] Tool Callingと生成キャンセルが動作する
- [x] oMLXのメモリ消費が統合メモリ管理に反映される
- [x] メモリ予算判定でoMLX使用分を考慮する
- [x] モデルを二重ロードしない
- [x] oMLX停止時も既存ランタイムは正常に動作する
- [x] アプリ再起動後にルーティング設定が復元される
- [x] 既存のMLXBarテストが引き続き成功する（654 passed）
- [x] 日本語・英語両UI完全対応

---

## 11. 既知の制限・今後の課題

1. **oMLXインストール管理**: 現版ではoMLXのインストール・更新はMLXBar外部で行う。将来的に管理API経由で可能にする。
2. **oMLX固有設定の二重管理防止**: PLE/MTP等の詳細設定はoMLX側で管理。MLXBar側は読取専用表示＋管理画面リンクのみ。
3. **生成キャンセル**: oMLXにサーバー側キャンセルAPIがないため、クライアント側ストリームクローズのみ。
4. **メモリ測定の精度**: oMLXの `/admin/api/stats` から取得。取得失敗時は0扱い（安全側へ倒す）。
5. **クラスタ/分散推論**: oMLXのクラスタ機能は未対応。

---

## 12. 検証記録

- Python回帰: **654 passed**（新規テストなし・既存全通過）
- Swift build: **成功**（0.15秒、警告なし）
- CLIコマンド: 全オプション動作確認
- 設定検証: 有効/無効/不正URL/不正routing等でバリデーション確認
- メモリ統合: `_resident_charge()` / `_admit()` でoMLX分が正しく加減算されることを確認
- ルーティング: `_is_omlx_model()` で正しく振り分けられることを確認
- 多言語UI: 日本語/英語で全文言表示確認