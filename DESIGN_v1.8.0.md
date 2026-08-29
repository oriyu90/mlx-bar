# MLXBar v1.8.0 設計書（同一モデルの並列常駐&生成 / Anthropic 互換 API）

更新日: 2026-08-30
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

1. **同一モデルの複数並列ロード&生成。** 1 つの MLX Worker プロセスは単一スレッドなので、同一モデルへの
   並列生成には **モデルのコピーを N プロセス常駐**させる（＝レプリカ）。
2. **Anthropic Messages API 互換（Claude Code 対応）。** Worker を増やさず、Coordinator に
   `/anthropic` 配下の入口/出口アダプタを追加する。実行部分は既存 Worker プールを共用する。

**非目的:** ランタイム挙動の変更。`generationConcurrency == 1` は v1.6.2 と、`models.pool.enabled == false`
は v1.6.1 と、それぞれ等価のまま。OpenAI 互換の入口（`/v1/models` ルーティング、`/v1/chat/completions`、
128 tools 上限、bearer 認証、SSE keep-alive コメント、エラー JSON 形）と設定 schema version 1 は不変。
`openai_compat.py` は 1 行も変更しない。

## 2. 不変条件（守ること）

- `models.pool.profiles[].replicas` 既定 1。既定のままなら `_slots` の内部キーは従来どおり bare `model_id`
  （replica 0）で、全コードパス・全既存テストが v1.7.1 と byte-identical。
- レプリカ 1..N は `instance_key` に `-<index>` サフィックスを付け、manifest / log / socket を分離する。
  replica 0 のパスは v1.7.1 と同一。
- admission の 4 層（事前予約 charge / global load lock / allocator 上限 / ロード後実測
  `max(RSS, active+cache)`）は per-replica に効く。N レプリカは N× `_cold_estimate` を charge し、
  `_resident_charge() + estimate > budget` で拒否する。`maxResidentModels` は常駐 Worker プロセス総数の上限。
- コールドロードは全体で 1 件ずつ（`loadConcurrency` 固定 `(1,1)`）。レプリカも `_load_lock` 下で直列。
- 生成 1 件は `PoolSlot.gen_lock`（そのレプリカ）→ プール全体 `asyncio.Semaphore(generationConcurrency)`
  → `_concurrent_start_ok()` の順で取得。同一モデルの複数要求は空きレプリカへ振り分け、全 busy なら
  最小キューのレプリカで待つ。プール全体の同時生成数は `generationConcurrency` が束ねる。
- 孤児レーン回復（`_recover_lane`）は per-replica で従来どおり機能する。
- Anthropic 入口は `app.mount("/anthropic", ...)` の sub-app。認証は `x-api-key` **または** bearer、
  `anthropic-version` 必須、エラーは `{"type":"error","error":{...},"request_id":"req_..."}` 形。
  ストリームに `[DONE]` を出さない。`reasoning_delta` は転送しない（署名 thinking を作らない）。
  `cache_control` は受理して無視し、`usage` に cache 系フィールドを出さない。応答 `model` は実ローカル名。
- `count_tokens` は Worker RPC の実トークナイザ計測。古いランタイムは `COUNT_TOKENS_UNAVAILABLE` で
  安全に失敗（概算を「正確な値」として返さない）。

## 3. アーキテクチャ

```text
Public Listener (make_public_app)
├─ 既存 OpenAI API (openai_compat, 無変更)  /v1/chat/completions /v1/models ...
├─ Anthropic sub-app  (app.mount("/anthropic"))          ← 新設・隔離
│   ├─ POST /anthropic/v1/messages                (anthropic_compat + anthropic_stream)
│   ├─ POST /anthropic/v1/messages/count_tokens
│   └─ GET  /anthropic/v1/models[/{id}]
└─ 共通実行部分（openai_compat から import して共用）
    _ensure_requested_model / _find_model / _is_generatable / resolve_public_images
    state.workers.generate_for_model() / .count_tokens() / .raise_if_queue_full()

ModelPoolSupervisor._slots: dict[replica_key -> PoolSlot]
    replica_key = model_id            (replica 0, v1.7.1 と同一)
                | f"{model_id}#{index}" (replica 1..N)
```

## 4. レプリカのライフサイクル

- **明示ロード**（GUI / CLI / preload、`pin=True`）: `_desired_replicas(model_id)` 個すべてを直列にロード。
  2 個目以降が admission で失敗しても replica 0 が生きていれば致命的ではない（`LOGGER.warning`、
  reaper が空き次第補充）。
- **API 自動ロード**（`pin=False`）: replica 0 のみロード。reaper の `_scale_up_pinned_replicas()` が
  `keep_loaded`／`session_pinned` なモデルを `desired` まで背景で補充する（要求は追加 admission の
  コストを払わない）。
- **スケールダウン**: `_reap_once` で、`replicas` を下げた／`maxReplicasPerModel` が下がった場合、
  replica 0 と leased を除く高インデックス側から `desired` まで evict。
- **TTL/圧力**: 非 pinned レプリカは個別に失効。critical pressure は既存どおり idle を解放。

## 5. Anthropic 変換の要点

`mlx-bar_anthropic_api_design.txt` §2/§3 に準拠。`system`→先頭 system message、text block→content、
`image`(base64/url)→`data:` URI または URL で `resolve_public_images`（private workspace・容量・SSRF
ガードを通す）、`tools[].input_schema`→OpenAI 風 function、`tool_use`→`assistant.tool_calls`、
`tool_result`→`role=tool` メッセージ、`tool_choice` auto/any/tool/none→auto/required/function/none、
`disable_parallel_tool_use`→`parallel_tool_calls=false`、`stop_sequences`→`stop`。

出力は `AnthropicMessageBuilder` 状態機械: `message_start` → `content_block_start`/`content_block_delta`
(`text_delta` / `input_json_delta`)/`content_block_stop` → `message_delta`(`stop_reason`/`stop_sequence`)
→ `message_stop`。heartbeat/queue/phase/progress→`ping`。ストリーム中 error→`event: error` で終端
（`message_stop` を出さない）。`stop_reason`: stop→end_turn / length→max_tokens / tool_calls→tool_use /
stop 文字列検出→stop_sequence（Worker の `completed` イベントに `stop_sequence` を追加、OpenAI 経路は無視）。

**v1 で未対応**（明示 `invalid_request_error`）: server-side tools、extended thinking（署名 block）、
PDF/`document` content block、Anthropic 側 MCP 実行。

## 6. 検証

`TEST_PLAN_v1.8.0.md` を参照。Python 回帰 **346** 件（v1.7.1 の 316 ＋ 新規 30）、Swift Debug/Release
ビルド成功。実機 GUI／推論／Claude Code 実接続は現場で実施（この環境に MLX ランタイム・モデル本体なし）。
