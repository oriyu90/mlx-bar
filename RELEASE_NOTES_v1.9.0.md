# MLXBar v1.9.0

Two menu-bar features for multi-model use, one pre-existing status bug fixed, and
an Anthropic / OpenAI / ZCode compatibility audit. No changes to the
OpenAI-compatible or Anthropic-compatible success-path response bodies, `usage`,
error shapes, or `code` values. Settings schema (version 1) unchanged.

メニューバーのマルチモデル向け機能を2つ、既存の状態表示バグを1つ修正、Anthropic /
OpenAI / ZCode の互換性精査。OpenAI互換 / Anthropic互換の成功経路のボディ・`usage`・
エラー形・`code` 値は不変。設定 schema（version 1）も不変。

## Added / 追加

### Unload resident models one at a time / 常駐モデルを1つずつアンロード

The menu bar's resident-model list (per-row eject and pin) now shows whenever at
least one model is resident, not only when two or more are. In every
configuration you can now release a single model without touching the others.
The backend (`POST /api/v1/models/{id}/unload`, `mlxbarctl model unload <id>
[--force]`) is unchanged from v1.7.0 / v1.8.1 — only the GUI's display condition
widened.

メニューバーの常駐モデル一覧（行ごとの取り出し／ピン）を、常駐が1つのときにも表示します
（従来は複数常駐時のみ）。どの構成でも、他のモデルに触れずに1モデルだけ解放できます。
バックエンドは v1.7.0 / v1.8.1 から無変更で、GUI の表示条件を広げただけです。

### Per-model tokens/sec while multiple models generate / 生成中のモデル別トークン速度

When more than one model is resident, each row in the resident-model list shows
that model's current tokens/sec beneath it while it is generating (idle rows show
nothing). `/api/v1/status` now carries `generationTokensPerSecond` (and
`generatedTokens`) per entry in `loadedModels[]`. For a model running several
replicas, the collapsed row shows the fastest replica's current rate. The value
uses the same `_live_generation()` measurement as the single-model header line
and the same rounding (integer at/above 10 tok/s, one decimal below), to avoid
the per-tick content churn that destabilises `MenuBarExtra`'s window style.

複数モデル常駐時、常駐モデル一覧の各行の下に、そのモデルが生成中のトークン毎秒を表示します
（アイドルの行には出しません）。`/api/v1/status` の `loadedModels[]` 各要素に
`generationTokensPerSecond`（と `generatedTokens`）をモデルごとに載せます。同一モデルを
複数レプリカで動かしている場合は、畳まれた行に最速レプリカの現在値を表示します。

## Fixed / 修正

### Generation-rate line was blank whenever the model pool was enabled / モデルプール有効時に生成速度が出ない不具合

`/api/v1/status`'s top-level `generationTokensPerSecond` was only emitted by the
single-worker path (`_live_generation()`); the pool path (enabled by default)
never emitted it, so `MenuBarViewModel.generationRateText` was always nil. The
pool path now mirrors the primary model's current rate into that field.

`/api/v1/status` のトップレベル `generationTokensPerSecond` は単一Worker経路でしか付与されて
おらず、既定で有効なプール経路では常に欠落していました。プール経路でも主モデルの現在値を
同フィールドへ反映します。

## Compatibility audit / API互換性の精査（Anthropic / OpenAI / ZCode）

- **Anthropic (`/anthropic`, for Claude Code)** — auth (`x-api-key` / bearer),
  required `anthropic-version`, error envelope + `request-id`, the SSE sequence
  (`message_start` → `content_block_*` → `message_delta` → `message_stop`,
  `ping`, `event: error`), `tool_use` / `tool_result` round-trip, `count_tokens`,
  and model listing are all spec-conformant and covered by tests.
  - **Change:** `thinking: {"type": "disabled"}` (no budget) is now accepted as a
    no-op instead of a blanket HTTP 400. Local models already behave that way, so
    no success-path output, `usage`, or error shape changes. Turning extended
    thinking **on** (`enabled` / `adaptive` / `budget_tokens`), server-side
    tools, `document` blocks, and Anthropic-side MCP are still explicitly
    rejected with 400 (a known v1 limitation).
  - Known limitations (unchanged): `top_k` is ignored; `cache_control` is
    accepted and ignored; no cache fields in `usage`; the streaming
    `message_delta.usage` also carries `input_tokens` (Anthropic sends only
    `output_tokens` there — a compatible superset that Claude Code parses fine).
- **OpenAI / ZCode** — `max_tokens` fallback, `stream_options.include_usage`,
  `tool_choice` variants, the 128-tool cap, `delta.role` once, `finish_reason`
  including `length`, OpenAI-shaped errors for `/v1/completions` and unknown
  paths, `thinking` / `reasoning_effort` / `extra_body.chat_template_kwargs`
  normalisation into both mlx-lm and mlx-vlm, SSE keep-alive, and lane recovery
  on disconnect were all verified. `response_format` (non-text), `logprobs`, and
  `n>1` remain explicit errors (known). No serious incompatibility found.
- **Other (out of scope for this release):** the timing-sensitive test
  `test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats`
  (0.03 s heartbeat interval) is flaky under a full parallel run (passes in
  isolation). Recorded in `mlx-bar.md`.

## Invariants / 不変条件

- OpenAI-compatible and Anthropic-compatible success paths (streaming and
  non-streaming): response body, `usage`, SSE event sequence, error `code` /
  `type`, HTTP status — all unchanged, except `thinking: {"type": "disabled"}`
  going from 400 to a 200 no-op.
- `/api/v1/status` gains fields only (`loadedModels[].generationTokensPerSecond`,
  `loadedModels[].generatedTokens`, top-level `generationTokensPerSecond`);
  existing fields keep their type and meaning; older GUIs stay compatible.
- With the pool disabled (`models.pool.enabled=false`), `status()` returns
  `_legacy.status()` verbatim — no change.
- `POST /api/v1/models/{id}/unload` and `mlxbarctl` are unchanged; only the GUI's
  display condition widened.
- `status()` additions are fail-safe (a missing value is omitted, never an
  exception); model eviction stays under `_load_lock`; `unload_model(force=True)`
  cancels only that model's generations.

## Verification / 検証

- Python regression suite: **376 passed** (373 from v1.8.4 + 3 new: per-model
  rate in status, no rate when idle, `thinking:disabled` no-op). Swift Debug /
  Release builds succeed.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware confirmation (per-model rate display, per-model unload, Claude Code
  / OpenAI smoke): see `TEST_PLAN_v1.9.0.md §2` — carried to the next person on
  Apple Silicon.
- Design and invariants: `DESIGN_v1.9.0.md`. Maintenance notes: `mlx-bar.md`.
- Apple notarization: not done (ad-hoc signature).

## Checksum

`87dcc6f46b32ba249c306ca0b174116be9111ee700705b50a91544bbb3f66195`  `MLXBar-1.9.0.dmg`

(Replace with the SHA-256 of the `dist/MLXBar-1.9.0.dmg` produced by
`build-release.sh`; also written to `dist/MLXBar-1.9.0.dmg.sha256`. DMG packaging
is not bit-reproducible — re-hash if rebuilt.)
