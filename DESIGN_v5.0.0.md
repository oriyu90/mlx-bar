# DESIGN v5.0.0 — APIフォールバック（外部AIプロバイダーへの自動委譲）

> 対象: MLXBar v4.0.2 からの追加機能。既定は**完全に無効**で、v4.0.2 と
> バイト等価の挙動を保つ（`apiFallback` を一度も設定していないインストールは
> 新コードパスへ到達しない）。
>
> 関連文書: `TEST_PLAN_v5.0.0.md`、`RELEASE_NOTES_v5.0.0.md`、`CHANGELOG.md` [5.0.0]

---

## 1. 目的（ユーザー要求の整理）

APIでリクエストが来たとき、MLXBar のキューが一杯、または MLXBar 側で応答できない場合に、
**別のAIプロバイダーのAPIへ MLXBar から転送**できるようにする。

要求項目（原文の意図を分解したもの）:

| # | 要求 | 本設計での対応 |
|---|---|---|
| R1 | キューが一杯のとき外部AIのAPIへさらにリクエストする | F-1（キュー満杯・レート上限・ローカル失敗の3トリガー） |
| R2 | 設定画面に「APIフォールバック」タブを作り、そこで設定する | F-6（SwiftUI 設定ページ） |
| R3 | 画像やコンテキストなどの設定も詳しく行える | F-3（画像転送・コンテキスト上限・生成パラメーター・タイムアウト） |
| R4 | 登録したフォールバックモデルごとに「mlx-bar のどのモデルをフォールバック前モデルとするか」を設定 | F-2（`sourceModels`。`*` で全モデル） |
| R5 | 全モデル選択時に、他で明示指定されたモデルはそちらを優先 | F-2（明示指定 > ワイルドカードの解決順） |
| R6 | 有効化すると、フォールバック元として選ばれていないモデルはフォールバックできない | F-2（`sourceModels` に一致しない要求はフォールバック対象外） |
| R7 | フォールバックモデルごとに送れるリクエスト数（レート）を設定 | F-4（`maxRequests` / `rateWindowSeconds`） |
| R8 | 全モデルを複数のフォールバックで選択した場合の優先順位を設定 | F-2（`priority` 昇順） |
| R9 | 画像非対応のフォールバックモデルは画像付き要求の転送先にならない。画像対応モデルにキューが生成されてもそちらへ送る | F-5（画像要求時は `supportsImages` のみ候補。キューを待たずに転送） |
| R10 | Anthropic / OpenAI Responses / OpenAI Chat Completions に対応 | F-3（`protocol`: `openai-chat` / `openai-responses` / `anthropic-messages`） |
| R11 | 複数プロバイダーを登録できる | F-2（`providers[]` の配列） |
| R12 | 安全な設計（互換性・既存機能・クラッシュ安全性・メモリ安全性） | §5 |
| R13 | 日本語・英語の完全対応UI | F-6（`LS()` キー + `en.lproj` 追加 + `check_i18n`） |

---

## 2. 現状設計の調査結果（v4.0.2 実測）

### 2.1 リクエスト経路

```
クライアント
  ├─ POST /v1/chat/completions        Coordinator/mlxbar/api/openai_compat.py: chat()
  ├─ POST /v1/completions             openai_compat.py: legacy_completions()
  ├─ POST /v1/responses               404 UNSUPPORTED_ENDPOINT（意図的）
  └─ POST /anthropic/v1/messages      Coordinator/mlxbar/api/anthropic_compat.py: _messages()
```

両経路とも次の順で処理する:

1. 入力正規化（メッセージ・tools・response_format・thinking）
2. `resolve_public_images()` — 画像参照を**私有ディレクトリの実ファイル**へ書き換える
   （`ImageWorkspace`。要求終了時に `cleanup()`）
3. `_ensure_requested_model()` — ロード済みモデルの再利用 → カタログ解決 → 自動ロード
4. `workers.raise_if_queue_full()` — `QUEUE_FULL`（429, retryable）を送出
5. `maybe_compress_messages` / `maybe_plan_adaptive_memory` / RAG 注入
6. `workers.generate_for_model(loaded_id, messages, images, options, request_id, image_root)`
   → ワーカーのイベント列（`delta` / `reasoning_delta` / `tool_calls` / `completed` /
   `usage` / `metrics` / `error` / `queue` …）
7. ストリーム時は SSE へ、非ストリーム時は `_run_one_completion()` で集約

### 2.2 キュー満杯の判定（既存）

- `Coordinator/mlxbar/workers/model_pool.py: raise_if_queue_full()`
  （`generation.maxQueuedRequests` 既定 16 を超えたら `QUEUE_FULL`）
- `Coordinator/mlxbar/workers/supervisor.py: raise_if_queue_full()`（プール無効時）
- `generate()` 内でも同じ判定を行い、`QUEUE_FULL` を送出する

→ **フォールバックの契機は「既存の `QUEUE_FULL` が起きる直前」で十分**。
新しい判定を増やさず、既存の判定関数をそのまま使う（既存機能の挙動を変えない）。

### 2.3 設定基盤

- `Coordinator/mlxbar/settings.py: DEFAULTS` — 全キーの既定値。`_validate()` が
  範囲・型を検証し、未知キーは 422。**設定は全量マージ後 JSON として原子的に保存される**
  （`_atomic_write` + fsync）。
- シークレット（APIキー）は `control/` 配下の 0600 ファイルに分離保存する既存方式がある
  （`api-token`、`lm-studio-token`、`rag-embedding-token`、`omlx-token`）。
  → 外部プロバイダーのAPIキーも**同じ方式**で 1 プロバイダー 1 ファイルに保存する。
  設定 JSON には `apiKeyConfigured: bool` だけを持たせ、平文キーを保存しない。
- 設定変更は `PUT /api/v1/settings`（部分マージ）と `SettingsStore.write_lock` + 409
  `SETTINGS_CONFLICT`（5 秒）で保護されている。

### 2.4 GUI

- `Sources/MLXBar/Settings/MLXBarSettingsView.swift` の `pages` 配列がサイドバーの
  タブ一覧。日本語リテラルがそのまま `LS()` のキー。
- `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` が `CoordinatorClient`（curl + UDS）で
  管理APIを呼ぶ。`json(_:_:_:)` ヘルパーと `perform {}` ヘルパーが共通。
- ローカライズ: 日本語リテラルがキー、`en.lproj/Localizable.strings` に英訳。
  `scripts/check_i18n.py` が `LS()` 参照キーの欠落を検出する（**必須ゲート**）。
- エラー表示は `CoordinatorErrorText.table`（`Sources/MLXBar/Services/ErrorText.swift`）に
  コード→日英を登録する。`Tests/test_v3_2_0_error_contract.py` が未登録コードを検出する
  （**必須ゲート**）。

### 2.5 CLI

- `Coordinator/mlxbar/cli.py` の `parser()` にサブコマンドを追加し、`execute()` で
  `client.request("GET"|"PUT"|"POST", "/api/v1/...")` を呼ぶ。
- `Tests/test_gui_settings_parity.py` が「GUI のパネルが送る形」と「CLI が送る形」の
  整合を固定している → **新設定は GUI と CLI の両方から同じ形で書けるようにする**。

### 2.6 既存の制約・落とし穴

| 制約 | 設計への影響 |
|---|---|
| `api.requireToken` の認証は公開APIのみ。管理APIは UDS 経由で認証不要 | プロバイダーAPIキーは管理API経由でのみ読み書きする（公開APIには出さない） |
| 画像は `ImageWorkspace` の私有ディレクトリに実ファイルとして置かれる | 外部へ送るときは**ファイルを読み直して base64 化**する。パスを外部に漏らさない |
| `ImageWorkspace.cleanup()` は要求終了時に必ず呼ばれる | フォールバック経路も**同じ `finally` の中で**完了させる（先にクリーンアップしない） |
| ストリームは最初のチャンクを送出するとステータスコードを変えられない | フォールバック判断は**最初のチャンクを送出する前**に行う |
| `_ensure_requested_model` は `ENGINE_BUSY`（別モデルが応答中）を返す | これもフォールバック契機に含める（ローカルで応答できないため） |
| `MODEL_NOT_FOUND` / `INVALID_REQUEST` は利用者の誤り | フォールバック**しない**（誤設定を隠さない） |
| 設定は再起動なしで反映される必要がある | フォールバック設定は**要求のたびに** `settings.data` から読む（ラッチしない） |

---

## 3. 機能設計

### F-1 フォールバックの契機（トリガー）

要求は次の順で評価し、**最初に成立した条件**で外部へ委譲する。

| 順 | トリガー | 条件 | 既定 |
|---|---|---|---|
| 1 | `queueFull` | 対象モデルのキューが満杯（`raise_if_queue_full()` が `QUEUE_FULL`） | 有効 |
| 2 | `rateLimit` | 対象モデルが `maxRequestsPerWindow` に達している（F-4） | 有効 |
| 3 | `localError` | ローカルが応答を開始できない（`ENGINE_BUSY` / `MODEL_NOT_LOADED` / `MEMORY_*` / `MODEL_LOAD_FAILED` / `QUEUE_TIMEOUT` / 生成開始直後の `error` イベント） | 有効 |

**画像付き要求の特例（R9）**: `images` が 1 件以上あるときは、`queueFull` の判定を
「キューが一杯」ではなく**「待ちが発生するか」**に置き換える（`queueFullOnImages`、
既定 `true`）。つまり画像対応の外部モデルがあれば、キューを生成して待つのではなく
**そのまま転送する**。画像対応の外部モデルが無い場合は従来どおり MLXBar のキューで待つ。

外部へ委譲した要求は、MLXBar 側のキューを**消費しない**（判定は転送前に行う）。

### F-2 プロバイダーとモデルの選択（R4・R5・R6・R8・R11）

```
apiFallback:
  enabled: false
  providers: [Provider]      # 複数登録可
  cooldownSeconds: 60        # 失敗したモデルをスキップする秒数
  maxAttempts: 3             # 1要求で試す外部モデルの上限
```

`Provider` は次のモデル一覧を持つ（UI 上は「フォールバックモデル」として1行ずつ表示）:

```
Provider:
  id: "p-xxxxxxxx"           # 生成・不変
  name: "OpenAI"             # 表示名
  protocol: openai-chat | openai-responses | anthropic-messages
  baseURL: "https://api.openai.com/v1"
  enabled: true
  timeoutSeconds: 120
  models: [FallbackModel]
```

`FallbackModel`:

```
FallbackModel:
  id: "m-xxxxxxxx"
  model: "gpt-4.1-mini"          # 外部APIへ渡すモデル名
  label: ""                      # 表示名（空なら model）
  enabled: true
  priority: 10                   # 小さいほど優先
  sourceModels: ["*"]            # MLXBar のモデル id / name / provider_key、または "*"
  sourceModelsExplicit: false    # ユーザーが明示的に選んだか（R6 の判定に使う）
  supportsImages: false          # 画像を受け付けられるか
  maxRequests: 0                 # ウィンドウ内の上限（0 = 無制限）
  rateWindowSeconds: 60
  maxOutputTokens: 0             # 0 = 要求の max_tokens をそのまま使う
  maxContextTokens: 0            # 0 = 無制限（超過時は古いメッセージを落とす）
  imageDetail: auto              # OpenAI の detail（auto/low/high）
  temperature: null              # null = 要求値をそのまま使う
  topP: null
  extraHeaders: {}
```

**解決順（R5・R8）**:

1. 有効なプロバイダー × 有効なモデルを集める。
2. 要求モデル（`body.model` を解決した MLXBar モデルの `id` / `name` / `provider_key`）に
   対して、
   - **明示一致**（`sourceModels` に要求モデルのいずれかの名前が含まれる）を集める
   - 明示一致が 1 件も無ければ、**ワイルドカード一致**（`sourceModels` に `*`）を集める
   → 明示一致があればワイルドカードは**完全に無視**する（R5 の「明示された方を優先」）。
3. 画像付き要求なら `supportsImages == true` だけに絞る（R9）。**絞った結果が空なら
   フォールバックしない**（画像対応の外部モデルが無いならローカルで待つ）。
4. `priority` 昇順 → 同順位は `providers[]` の並び順 → さらに同順位は `models[]` の並び順。
5. クールダウン中（直近 `cooldownSeconds` 内に失敗した）モデルを末尾へ回す（除外はしない。
   全滅時は最古の失敗モデルを再試行する）。
6. レート上限（F-4）に達しているモデルを末尾へ回す。
7. 先頭から最大 `maxAttempts` 件を順に試す。

**R6（有効化時に未選択モデルはフォールバック不可）**: 上記 2. の結果が空なら
フォールバックは行わない。`sourceModelsExplicit` は UI の「すべてのモデル」チェックの
状態を表すだけで、解決順は `sourceModels` の内容だけで決まる（設定の意味が1つに定まる）。

### F-3 外部プロバイダーとのプロトコル（R3・R10）

`Coordinator/mlxbar/fallback/` に純粋な変換層を置く（FastAPI に依存しない）。

```
fallback/
  __init__.py
  config.py      # 設定の正規化・検証・既定値・解決（純粋関数）
  selector.py    # 候補選択（F-2）とレート制御（F-4）
  translator.py  # 内部 messages/options -> 各プロトコルの要求JSON
  client.py      # httpx による送信・SSE解析・イベント正規化
  limiter.py     # レート制御（メモリ内・有界）
```

**共通の入力**: 内部メッセージ（`role` / `content`（文字列 or `[{type:text|image_url}]`）/
`tool_calls` / `tool_call_id`）、`options`（`max_tokens` / `temperature` / `top_p` / `stop` /
`tools` / `tool_choice`）、画像（`ImageWorkspace` の実ファイル）、要求モデル名。

**出力**: 既存のワーカーイベントと**同じ形**の async イベント列
（`delta` / `reasoning_delta` / `tool_calls` / `completed` / `usage` / `error`）。
これにより、上流（`openai_compat.chat` / `anthropic_compat._messages`）の
SSE 組み立てコードは**一切変更せずに**外部応答を流せる（互換性の要）。

| protocol | エンドポイント | 画像 | tools | ストリーム |
|---|---|---|---|---|
| `openai-chat` | `POST {baseURL}/chat/completions` | `image_url`（data URI） | `tools` / `tool_choice` | SSE `data:` / `[DONE]` |
| `openai-responses` | `POST {baseURL}/responses` | `input_image`（data URI） | `tools`（function） | SSE `response.*` イベント |
| `anthropic-messages` | `POST {baseURL}/messages` | `image`（base64 source） | `tools` / `tool_choice` | SSE `content_block_delta` 等 |

- 認証: `openai-*` は `Authorization: Bearer`、`anthropic-messages` は `x-api-key` +
  `anthropic-version: 2023-06-01`。`extraHeaders` で上書き可。
- `baseURL` は `http://` / `https://` のみ。**ループバック（自分自身）への指定は拒否**
  しない（LM Studio 等のローカル外部サーバーをフォールバック先にしたい正当な用途が
  あるため）。ただし MLXBar 自身の公開ポートを指す設定は**検証で警告**する。
- 画像は MLXBar が受け取ったものを**そのまま** base64 で送る（再エンコードしない）。
  1 枚あたり `generation.maxImageBytes`、合計は `maxImages` で既に制限済み。
- コンテキスト上限: `maxContextTokens > 0` のとき、概算トークン（文字数/4、画像は
  1枚 1024）で上限を超えていれば**古いメッセージから落とす**（system は常に残す）。
  落としたことは `request.state.api_log["fallbackDroppedMessages"]` に記録する。
- タイムアウト: `timeoutSeconds`（接続 10 秒、読み取りはストリームのチャンク間）。
  総経過が `timeoutSeconds` を超えたら打ち切る。

### F-4 レート制御（R7）

- モデル単位で「ウィンドウ内の送信数」をメモリ内に保持する（`limiter.py`）。
  キーは `providerId/modelId`。**有界**（最大 256 エントリ、超過時は最古を破棄）。
- `maxRequests == 0` は無制限。`rateWindowSeconds` は 10〜86400。
- 判定は候補選択時（F-2 の 6.）に行い、上限到達モデルを末尾へ回す。
  全候補が上限到達ならフォールバックしない（`FALLBACK_RATE_LIMITED`）。
- カウントは**送信を開始した時点**で増やす（失敗しても消費する。暴走を防ぐため）。
- 再起動でリセットされる（永続化しない。設定 JSON を肥大させないため）。

### F-5 失敗時の扱い

- 外部モデルが HTTP エラー・タイムアウト・プロトコル不正で失敗したら、
  そのモデルをクールダウンに入れ、**次の候補**を試す（最大 `maxAttempts`）。
- 全候補が失敗したら、**元のローカルエラー**（`QUEUE_FULL` 等）をそのまま返す。
  外部のエラーは `detail["fallbackAttempts"]` に付ける（既存クライアントの
  `error.code` / `error.message` の解釈を壊さない）。
- **ストリーム中に**すでにチャンクを送出した後の失敗は、既存の `error` イベントで
  通知する（再送しない。二重出力を避けるため）。
- `asyncio.CancelledError`（クライアント切断）はフォールバックしない・再試行しない。

### F-6 GUI（R2・R13）

設定画面のサイドバーに **「APIフォールバック」** を追加する（`APIサーバー` の直後）。

- 有効化トグル、共通設定（クールダウン秒・最大試行数・画像要求時は即時転送）
- プロバイダーカード（追加・削除・有効化・名前・プロトコル・ベースURL・APIキー・
  タイムアウト・接続テスト）
- プロバイダー配下のフォールバックモデル行（追加・削除・有効化・モデル名・優先順位・
  画像対応・レート上限/ウィンドウ・最大出力トークン・最大コンテキスト・画像detail・
  温度・top_p・フォールバック元モデル選択）
- フォールバック元モデル選択は **「すべてのモデル」チェック + 個別モデルの複数選択**
  （カタログのモデル一覧から）
- 状態表示: 直近のフォールバック（時刻・理由・送信先）を `GET /api/v1/fallback/status` から

すべての文言は `LS("日本語")` をキーにし、`en.lproj` へ英訳を追加する。

### F-7 管理API（GUI/CLI の共通経路）

| メソッド | パス | 用途 |
|---|---|---|
| `GET` | `/api/v1/fallback` | 設定 + シークレット有無 + 直近の使用状況 |
| `PUT` | `/api/v1/fallback` | 設定の部分更新（`apiFallback` の下だけ） |
| `POST` | `/api/v1/fallback/providers` | プロバイダー追加（`id` を採番） |
| `DELETE` | `/api/v1/fallback/providers/{id}` | プロバイダー削除（APIキーファイルも削除） |
| `POST` | `/api/v1/fallback/providers/{id}/models` | モデル追加（`id` を採番） |
| `DELETE` | `/api/v1/fallback/providers/{id}/models/{modelId}` | モデル削除 |
| `PUT` | `/api/v1/fallback/providers/{id}/api-key` | APIキー保存（空で削除） |
| `POST` | `/api/v1/fallback/providers/{id}/test` | 接続テスト（`/models` 相当の軽い呼び出し） |
| `GET` | `/api/v1/fallback/status` | 直近のフォールバック実行・レート使用状況 |

- 追加・削除・APIキーは `SettingsStore.write_lock` の下で行う（`SETTINGS_CONFLICT` 409）。
- 設定の**検証は `SettingsStore._validate` に一本化**する（GUI・CLI・API のどこから
  書いても同じ規則）。
- 公開API（`/v1/*`）には**フォールバック設定を一切露出しない**。

---

## 4. 変更ファイル一覧

| 種別 | ファイル | 内容 |
|---|---|---|
| 新規 | `Coordinator/mlxbar/fallback/__init__.py` | 公開 API |
| 新規 | `Coordinator/mlxbar/fallback/config.py` | 設定の既定値・正規化・検証・解決 |
| 新規 | `Coordinator/mlxbar/fallback/selector.py` | 候補選択・解決順 |
| 新規 | `Coordinator/mlxbar/fallback/limiter.py` | レート制御・クールダウン |
| 新規 | `Coordinator/mlxbar/fallback/translator.py` | 内部形式 → 各プロトコル |
| 新規 | `Coordinator/mlxbar/fallback/client.py` | httpx 送信・SSE 解析・イベント正規化 |
| 新規 | `Coordinator/mlxbar/api/fallback.py` | 管理API ルータ |
| 変更 | `Coordinator/mlxbar/settings.py` | `DEFAULTS["apiFallback"]`、`_validate`、APIキー保存 |
| 変更 | `Coordinator/mlxbar/state.py` | `FallbackService` を `AppState` に追加 |
| 変更 | `Coordinator/mlxbar/api/openai_compat.py` | 契機判定と委譲（chat / legacy_completions） |
| 変更 | `Coordinator/mlxbar/api/anthropic_compat.py` | 契機判定と委譲（messages） |
| 変更 | `Coordinator/mlxbar/api/management.py` | ルータ登録 |
| 変更 | `Coordinator/mlxbar/cli.py` | `fallback` サブコマンド、`secrets` 追加 |
| 変更 | `Sources/MLXBar/Settings/MLXBarSettingsView.swift` | タブ追加・新ビュー |
| 変更 | `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` | フォールバック操作・状態 |
| 変更 | `Sources/MLXBar/Resources/en.lproj/Localizable.strings` | 英訳追加 |
| 変更 | `Sources/MLXBar/Services/ErrorText.swift` | 新エラーコードの日英 |
| 新規 | `Tests/test_v5_0_0_fallback.py` | 単体・統合テスト |
| 新規 | `Tests/MLXBarTests/FallbackSettingsTests.swift` | Swift 側の純粋関数テスト |

---

## 5. 安全性・互換性の設計判断

### D-1 既定で完全に無効

`apiFallback.enabled` の既定は `false`。`enabled == false` のとき、公開APIの経路は
`FallbackService` を呼ばない（`if` 1 回で抜ける）。v4.0.2 と同じ経路・同じエラーコード。

### D-2 既存のエラー契約を壊さない

- フォールバックしない要求は、v4.0.2 と**同一の例外**が同じ位置から送出される。
- フォールバックが全滅した場合も、**元のローカルエラー**を返す（`code` / `status` /
  `retryable` 不変）。外部の試行結果は `detail` に追加するだけ（additive）。
- 外部応答は既存のワーカーイベント形に正規化するため、SSE 組み立てコードは不変。

### D-3 クラッシュ安全性

- 外部通信はすべて `try/except Exception` で包み、**フォールバック経路の失敗が
  サーバーを落とさない**。`asyncio.CancelledError` は再送出する。
- 設定の書き込みは既存の `_atomic_write`（一時ファイル + fsync + rename）を再利用。
- APIキーは既存の `_write_secret`（0600 + fsync）を再利用。**設定 JSON には保存しない**。
- プロバイダー削除時は、設定更新が成功してからキーファイルを消す（順序を逆にすると
  設定だけ残ってキーが消えた壊れた状態になる）。

### D-4 メモリ安全性

- 外部応答はストリームで読み、**蓄積しない**（`delta` ごとに上流へ渡す）。
  非ストリーム経路でも、ツールコール引数の組み立てバッファに上限（1 MiB）を設ける。
- SSE の行バッファに上限（1 MiB）を設け、超過したら `FALLBACK_PROTOCOL_ERROR`。
- レート制御・クールダウンの表は有界（256 エントリ）。
- 画像はファイルから読み直して base64 化し、送信後に参照を落とす（要求スコープ）。

### D-5 プライバシーの明示

フォールバックは**プロンプトと画像を外部へ送る**機能である。UI に
「有効にすると、対象リクエストの内容（プロンプト・画像）が選択した外部プロバイダーへ
送信されます」を常時表示し、有効化の確認ダイアログを出す。README・紹介サイトにも明記する。

### D-6 画像付き要求の扱い（R9 の解釈）

「画像に対応していないフォールバックモデルは画像付きのリクエストのフォールバック先には
選ばれず、画像付きのモデルにキューを生成してでもそちらにリクエストする」を次のように実装する:

- 候補は `supportsImages == true` のみ。
- 候補が 1 件以上ある → **キューを待たずに外部へ送る**（画像対応モデルを選び、
  必要ならキューを生成してでもそちらへ、という要求の意図を「外部の画像対応モデルへ
  即時転送」として満たす）。
- 候補が 0 件 → ローカルのキューで待つ（＝MLXBar の画像対応モデルが処理する）。

この解釈は UI の説明文とリリースノートに明記する。

---

## 6. テスト方針

| 種別 | 内容 |
|---|---|
| 単体 | 設定の既定値・検証（型/範囲/重複ID/`*` と明示の併存）、解決順（明示 > ワイルドカード、priority、同順位の安定性） |
| 単体 | 画像フィルタ（`supportsImages` のみ、0 件ならフォールバックしない） |
| 単体 | レート制御（ウィンドウ内の上限、0=無制限、有界、クールダウン） |
| 単体 | 変換（3プロトコル × テキスト/画像/tools/stop/max_tokens） |
| 統合 | 偽HTTPサーバー（`httpx.MockTransport`）で 3 プロトコルの正常・エラー・中断 |
| 統合 | `/v1/chat/completions` が `QUEUE_FULL` 時にフォールバックし、SSE が正しい形で流れる |
| 統合 | `/anthropic/v1/messages` が同様に動く |
| 回帰 | `enabled=false` で v4.0.2 と同一の応答・エラー |
| 回帰 | 既存 939 テストが全パス |
| GUI | `check_i18n` 欠落 0、`swift test` 全パス、`CoordinatorErrorText` 登録漏れ 0 |

---

## 7. 実装順（作業パッケージ）

1. WP-1 設定スキーマ・検証・シークレット（`settings.py`）
2. WP-2 `fallback/config.py` `selector.py` `limiter.py`（純粋ロジック）
3. WP-3 `fallback/translator.py` `client.py`（プロトコル）
4. WP-4 `fallback/__init__.py`（`FallbackService`）+ `state.py` 配線
5. WP-5 `openai_compat.py` / `anthropic_compat.py` 統合
6. WP-6 `api/fallback.py` + `management.py` 登録
7. WP-7 `cli.py`
8. WP-8 Swift GUI + ローカライズ + ErrorText
9. WP-9 テスト
10. WP-10 ドキュメント・リリース・Web
