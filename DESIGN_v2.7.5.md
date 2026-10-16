# DESIGN v2.7.5 — アプリ本体の省メモリ / リーク精査 / クラッシュ安全性

- 版数: app 2.7.5 / build 52。Coordinator `__version__` 2.7.5。`schemaVersion` は 1 のまま（追加のみ）。
- 方針: 既定値不変、追加・上限・ガードのみ、検証失敗は fail-closed。Coordinator が正本、GUI/CLI は薄いクライアント。
- v2.7.4 の厳格解決（別名形優先・キュー＋逼迫時の非常駐即時解放）と v2.7.3 の厳格上限は維持する。Swift の UI 文言・キー追加なし（日英完全対応は不変）。

## 1. 背景

- オーナー依頼: (1) mlx-bar アプリ自体のメモリ使用量をできるだけ少なく、(2) メモリリーク精査（発見→原因→計画→修正）、(3) クラッシュ安全性・動作安定性精査（発見→原因→計画→修正）。詳細な作業プランを立てた後作業→設計評価とデバッグ→release・Web更新。安全設計と日英完全対応UIを守る。
- 精査結果: Swift の force-unwrap/`try!`/範囲外は既にゼロ（硬化済み）。Python 要求経路の例外は 400/422/429/500 へ degrade し pool slot は常に解放、Worker の paged/tool/MLX 経路は cold/EXACT/`GENERATION_FAILED` へ fail-open（circuit breaker 付き）。残存リスクは (a) SQLite の `jobs`/`runtime_history` 無制限蓄積、(b) Swift `chatOutput` 無制限、`StreamBuffer` 無制限、(c) 毎秒 `status()` の `getaddrinfo`＋SQL fan-out、(d) `coordinator-crash.log` 無制限、(e) 起動時 sqlite open 失敗の crash-loop、(f) `status()` の数値変換・Worker 不正行・非文字列 `text`/非整数 `usage` の 500（poisoned worker 残留含む）、(g) Swift `stream()` の cancel で curl が `--max-time 7210` まで残る hang。

## 2. 修正内容

### 2.1 アプリ本体の省メモリ（いずれも既定値・形状不変）

- Swift `chatOutput` 上限 200,000 文字（tail 保持）。`MenuBarViewModel.maxChatOutputCharacters`。表示中の応答は保たれる。設定キー・文言追加なし。
- `StreamBuffer` 上限（pending 1MiB・diagnostic 64KiB、古い側から破棄）。wedged SSE でも Swift RSS は膨らまない。`data:` 行の parse は不変。
- `recentLogs` 取得 `limit=500→200`（server 保持 2,000 は不変）。`logStatus` 文言は件数動的のため日英不変。3 重コピー（DB→server→Swift dict→表示）を約 2/5 へ。
- `GET /status` の TTL cache（`management.py:_status_cache`）。LAN 5秒・RAG/tiers 3秒、tiers は model 別最大 32 key。JSON 形状同一。1秒ポーラーの `getaddrinfo`＋`list_collections`（COUNT×2）＋`recent_cache_tiers`（LIMIT 12）を削減。失敗は fresh read へ、stale error は返さない。
- `coordinator-crash.log` 1MB ローテーション（worker log と同一）。唯一の無制限ログを有界化。

### 2.2 リーク精査の修正（無制限→有界）

- `jobs` terminal（completed/failed/cancelled）最新 200 件保持。`upsert_job` の terminal 書き込み時と `fail_incomplete_jobs`（起動時）に sweep。active（queued/running）は不触。
- `runtime_history` engine 別最新 100 件保持。`add_runtime_history` 毎に当該 engine を sweep＋起動時に全 engine sweep。`list_*` の LIMIT 読みは不変。
- 起動時 `PRAGMA wal_checkpoint(TRUNCATE)`（best-effort）。WAL/SHM の残留を回収。
- Worker `server.py:lines()` の `pending` を `finally` で `cancel()`。切断時に `on_mlx_thread(next_event)` が MLX 単一スレッドを 1 event 分占有する残留を解消。正常完了時（done）は no-op。
- 上記 2.1 の `chatOutput`/`StreamBuffer` caps もリーク対策を兼ねる。
- 対応済み確認（変更なし）: `api_logs` 2,000（±100 drift）、`CancellationRegistry` 256/900s、`LRUPromptCache`（4 entries/10% RAM）、disk budget/sweep、paged per-block prune＋budget＋circuit、httpx `async with`、sqlite single-conn＋RLock。

### 2.3 クラッシュ安全性・安定性（degrade は 500、crash-loop なし）

- `Database.__init__` の open 失敗時に `-wal`/`-shm` 除去→1回 retry、それでも失敗は明確な例外（caller が emergency log＋非ゼロ exit で launchd 再起動）。起動前の半開動作を作らない。
- `supervisor.effective_max_tokens/effective_max_prompt_characters` の `int()` を guard（fallback 8192/100000）。破損 in-memory でも `/status` 1秒ポールが 500-storm にならない。
- `supervisor.generate` の `json.loads(line)` を guard。不正行は `WORKER_CRASHED`＋`_stop_worker`＋`loaded=None`（transport break と同一）。poisoned worker を残さない。slot は `finally` 解放のまま。
- `supervisor._call` の `response.json()` を guard（同上 `WORKER_CRASHED`）。
- `openai_compat._run_one_completion` の `text += str(...)` 化、`_usage_from_event` の int 安全化（garbage→0、`cached_tokens` 不正→field 省略）。worker 文字列 `"abc"` でも 500 で終わり process crash なし。
- `management.clear_memory_prompt_cache` に `MLXBarError` catch を追加（siblings と対称）。global `logged_internal_error` の二重安全のまま。
- Swift `stream()` に `withTaskCancellationHandler { process.terminate() }`。放棄された QuickChat Task が curl を 7210秒残さない。既存の `defer` cleanup・`SingleResume` は維持。

## 3. 互換性・安全性の不変条件

- 設定スキーマ追加なし（`DEFAULTS` 不変、`schemaVersion` 1）。既定値不変。新エラーコードなし。新 `.strings` なし。新規 `.fixedSize` なし。
- `enabled=false` は v1.6.1 へ完全委譲のまま。`generationConcurrency=1` は直列のまま。replica 0 常残・lease 保護・TTL/LRU は不変。
- 拒否・解放はプール状態を壊さず要求の成否のみに影響。解放が queue 待機を壊さない（lease 保護）。
- UI: Swift 文言・キー不変。`logStatus` の件数表示は動的のため 200 件でも日英正しい。

## 4. 検証（`TEST_PLAN_v2.7.5.md` に記録）

- 新規 `Tests/test_v2_7_5_memory_stability.py` 7件（jobs/history 有界・prune 最新保持・usage garbage 耐性・破損 settings fallback・status cache 同形状＋cache hit・clear_memory guard）。
- 既存回帰 607 passed（新規 7 含む、fluke なし）。
- `swift build` 成功。`.strings` 新規重複なし。新規 `.fixedSize` なし。
- 実機（M1 Max 64GB、gpt-oss-20b-MXFP4-Q8 11GB）: load→CLI generate→OpenAI chat（explicit model、max_tokens 16）→直後 status は active 0・lock idle・leases 0＝待機復帰。unload で resident 0。`promptCacheHealth`/`usage.prompt_tokens_details` 正常。
