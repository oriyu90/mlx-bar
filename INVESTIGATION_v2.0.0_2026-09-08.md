# MLXBar v2.0.0 — 現在状態と問題調査

調査日: 2026-09-08 (JST)  
対象: `/Applications/MLXBar.app`、MLXBarCoordinator、公開 OpenAI 互換 API

## 結論

公開API・管理API・ロード済みモデルは現在正常に動作している。WAKARU から観測した
「接続拒否」は、調査用シェルにローカルネットワーク接続権限が無かったことによる観測側の
問題であり、MLXBar の公開リスナー障害ではなかった。

一方で、**同一モデルのレプリカ数を2に設定しても、`maxResidentModels = 1` のときは
2台目が必ず拒否される実装上の不整合**を確認した。UI・設定名・ステータスが示す「常駐
モデル数」は異なるモデルの数であるのに、入場制御だけがレプリカを含む Worker スロット数を
数えている。安全余裕が十分あっても同一モデルの並列生成能力が失われるため、修正対象である。

## 実測した現在状態

| 項目 | 結果 |
|---|---|
| アプリ | MLXBar v2.0.0、ad-hoc 署名 |
| Coordinator | ServiceManagement の `com.yukiorita.MLXBar.Coordinator` が running |
| 管理API | Unix domain socket 経由の `/api/v1/health` が `status: ok` |
| 公開API | `127.0.0.1:11435/health` と LAN アドレスの `/health` がともに `status: ok` |
| 公開設定 | `0.0.0.0:11435`、認証必須、OpenAI/Anthropic 互換API有効 |
| モデル | `Qwen3.8-27B-MLX-4bit` を `mlx-vlm` Worker でロード済み（text / image、keep loaded） |
| 生成スモーク | OpenAI Chat Completions に `max_tokens: 8` を指定し、期待した短い応答・`finish_reason: stop`・usage を確認 |
| クラッシュ | coordinator crash log と新規の macOS crash report はなし |
| 最新APIエラー | 認証なしの `/v1/models` に対する想定どおりの 401 のみ |

APIキー、モデルパス以外の個人情報、リクエスト本文はこの記録に保存していない。

## 問題 1 — 同一モデルの追加レプリカが常駐モデル上限で拒否される

**優先度: 高（機能設定と実効性能の不一致）**

### 再現済みの状態

- `models.pool.maxResidentModels = 1`
- 同じ固定モデルの profile に `replicas = 2`
- `maxReplicasPerModel = 2`
- 現在のメモリ予約は1台あたり約20.7 GiB、全体予算は約83.5 GiB、OSの空きメモリも十分

期待値は「異なるモデルは1種類まで、同じモデルの Worker は2台まで常駐」である。しかし実際は
1台目のみロードされ、ログに `Could not load replica 1 ... keeping 1 replica(s)` が残る。

### 原因

`Coordinator/mlxbar/workers/model_pool.py` の `_admit()` は、`maxResidentModels` の判定に
`len(self._slots)` を使っている（328–335行）。`_slots` はモデル数ではなく、同一モデルの
レプリカも含む Worker スロット数である。

そのため、同一モデルの2台目を追加すると、メモリ予算は満たしていても
`len(self._slots) >= maxResidentModels` となり、`MEMORY_BUDGET_EXCEEDED` で拒否される。

これは次の公開契約と矛盾する。

- 設定UIは `maxResidentModels` を「最大常駐モデル数」と表現している
  (`Sources/MLXBar/Settings/MLXBarSettingsView.swift:261`)。
- ステータスも `residentModelCount` をユニークな model ID 数として返している
  (`model_pool.py:1088–1091`)。
- レプリカ機能は「同一モデルの独立 Worker」を意図している
  (`model_pool.py:360–377`)。

### 影響

- 同一モデルへの要求は実質1レーンで直列化される。
- `replicas = 2` を設定しても、ユーザーには「メモリ不足の可能性」としか見えず、設定意図を
  達成できない。
- WAKARU を含む複数クライアントの同時利用時に、設定した並列性能を発揮できない。

### 修正案

1. `maxResidentModels` はユニークな model ID 数だけを数える。追加対象の model ID が既に
   常駐している場合、モデル数上限では拒否しない。
2. Workerごとの予約メモリ、OSメモリ圧、モデルごとの上限、`maxReplicasPerModel` の検査は
   現状どおり各レプリカに適用する。つまりメモリ安全性は弱めない。
3. 退避対象を選ぶ際も「ユニークモデル数を減らす退避」と「同一モデルの余剰レプリカを退避
   する」場合を区別する。
4. 以下の回帰テストを `Tests/test_model_replicas.py` に追加する。
   - `maxResidentModels=1`、安全なメモリ予算、`replicas=2` で2 Worker がロードされる。
   - 同じ条件で別モデルを追加すると拒否される。
   - 2レプリカ分のメモリ予算が不足すると、既存どおり1台目だけで安全に稼働する。

## 問題 2 — 追加レプリカの失敗理由が診断から失われる

**優先度: 中（運用・デバッグ性）**

追加レプリカのロード失敗は `model_pool.py:370–377` で意図的に致命扱いを避けているが、
ログにはレプリカ番号とモデルIDしか残らない。例外の安定コード、メッセージ、メモリ見積り、
実効予算、OS pressure は失われる。管理APIの status / diagnostics にも「希望2・実際1・拒否
理由」がない。

今回の問題1では、実際にはモデル数上限が原因なのに、ログからはメモリまたは圧力による
正常な縮退と区別できなかった。

### 修正案

- `PoolSlot` または pool status に、モデルごとの `desiredReplicaCount`、
  `readyReplicaCount`、`lastReplicaAdmissionFailure`（安定 code と安全な message）を追加する。
- best-effort 失敗時の warning に code と安全な要約を含める。トークン・パス・リクエスト内容は
  記録しない。
- Settings の常駐数表示に `1 / 2 replicas` と理由を出し、ユーザーが設定の未達を把握できる
  ようにする。

## 問題 3 — 実効同時生成数と保存設定の差がステータスで明示されない

**優先度: 低〜中（操作の明確性）**

保存設定は `generationConcurrency = 3` だが、稼働中の Coordinator は起動時に読み込んだ
`generationConcurrency = 2` を使っている。これは仕様どおりで、値は Coordinator の生存期間中に
ラッチされる（`model_pool.py:69–76`、UIの再起動案内 `MLXBarSettingsView.swift:283`）。

しかし status の `modelPool.restartRequired` は pool 有効/無効の差だけを判定しており
（`model_pool.py:1085–1095`）、同時生成数の設定差を含まない。そのためAPI/診断だけを見る利用者は
設定が未反映であることを判断できない。

### 修正案

- `configuredGenerationConcurrency` と `effectiveGenerationConcurrency` を同時に返す。
- `restartRequired` を、pool 有効/無効 **または** 同時生成数が実効値と異なる場合に true にする。
- GUIでは再起動が必要な場合に、適用済み値と次回起動後の値を併記する。

## 注意事項 — 最大トークン数の現在値

`generation.maxTokens` は 262,131 で、ロード済みモデルの 262,144 トークン上限にほぼ等しい。
これはクラッシュではなく設定値だが、`max_tokens` を送らない外部クライアントでは非常に長い生成、
タイムアウト、不要なメモリ使用を招き得る。今回のスモークでは安全のため `max_tokens: 8` を
明示した。WAKARU は自身のリクエストで上限を指定するため直ちに影響しない。

今後は、UIでモデル上限に近い既定値へ確認表示または警告を出すこと、通常用途向けの控えめな
既定値に戻すことを検討する。互換性のため、既存設定を無断で書き換えない。

## 今回は問題ではなかった点

- 公開リスナーの bind / LAN公開: 正常。`0.0.0.0:11435` で待受し、loopback・LANの両方から
  health が成功した。
- 認証: 有効。認証なしモデル一覧への 401 は期待どおり。
- 管理Unix socket、Coordinator、ロード済み Worker: 正常。
- ロード済みモデルの最小 Chat Completions: 正常。
- 新規クラッシュ、モデル Worker の致命ログ、メモリ逼迫: 今回の時点ではなし。

## 推奨する次の作業順

1. 問題1を先に修正し、レプリカ数と常駐モデル数の意味をコード・テスト・診断で一致させる。
2. 問題2の診断情報を追加し、将来の安全な縮退と実装不具合を即座に区別できるようにする。
3. 問題3の実効値と再起動要否を status / GUI へ反映する。
4. 単体テスト、Swift build、既存のリリース検証、実機での「同一モデル2レプリカ・同時2要求」
   を実行する。APIトークン、リクエスト本文、個人パスを成果物に含めない。
