# MLXBar v1.8.0

Two features on top of v1.7.1. The model pool's default behavior is unchanged:
`models.pool.profiles[].replicas` defaults to 1 (one process per model, exactly
as before), `generationConcurrency=1` stays byte-equivalent to v1.6.2, and
`models.pool.enabled=false` stays byte-equivalent to v1.6.1. The OpenAI surface
is untouched — `openai_compat.py` has zero changes.

## Same-model parallel residency and generation / 同一モデルの並列常駐&生成

One MLX worker process is single-threaded, so running two generations of the
*same* model at once needs two copies of that model resident. A pinned profile
can now ask for that:

```json
{"models":{"pool":{"profiles":[
  {"modelId":"MODEL_ID","keepLoaded":true,"replicas":2}
]}}}
```

- Each replica is an independent worker process with its own memory reservation,
  manifest, log and socket. `replicas` is clamped to
  `models.pool.maxReplicasPerModel` (default 2, range 1–8).
- Admission charges each replica separately against the pool budget, so a second
  copy is rejected (`MEMORY_BUDGET_EXCEEDED`) if the two together do not fit —
  the model stays usable on the replica that did load.
- Concurrent requests to one model are routed to distinct replicas; the pool's
  `generationConcurrency` semaphore still caps total concurrent generations, and
  the memory head-room guard still applies to the 2nd+ lane.
- An explicit load (GUI / CLI / preload) brings every configured replica up now;
  an API autoload brings one copy up and the reaper tops a pinned model up in
  the background, so a request never pays the extra admission cost. Lowering
  `replicas` trims the highest-index idle replicas.
- `mlxbarctl model pin <id> --replicas N`; menu bar shows a `×N` badge; Settings
  has a per-model replicas stepper.

`replicas > 1` requires `models.pool.enabled: true`. The combined multi-copy
allocation peak has **not** been measured on hardware in this release
(`TEST_PLAN_v1.8.0.md §2`); the memory guard keeps a request that would have
succeeded serially from failing.

## Anthropic Messages API / Claude Code

An Anthropic-compatible surface is served under `/anthropic`, isolated from the
OpenAI one (its own auth, error envelope and SSE encoding). Point Claude Code at
it:

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:11435/anthropic
export ANTHROPIC_AUTH_TOKEN="$(cat ~/Library/Application Support/MLXBar/control/api-token)"
export ANTHROPIC_MODEL="<a model id from GET /anthropic/v1/models>"
```

- `POST /anthropic/v1/messages` (streaming and non-streaming), `POST
  /anthropic/v1/messages/count_tokens`, `GET /anthropic/v1/models[/{id}]`.
- Auth accepts `x-api-key` or `Authorization: Bearer`; `anthropic-version` is
  required; every response carries a `request-id` header.
- Translated: `system`, text, base64/URL images (through the same private
  workspace + size + SSRF guards as the OpenAI path), client tool use and
  `tool_result`, `tool_choice` auto/any/tool/none, `disable_parallel_tool_use`,
  `stop_sequences`. Streaming follows the `message_start` → `content_block_*` →
  `message_delta` → `message_stop` sequence with `ping` keep-alives and no
  `[DONE]`. `stop_reason` maps stop→end_turn, length→max_tokens,
  tool_calls→tool_use, and a matched stop sequence → stop_sequence.
- `count_tokens` uses the loaded model's real tokenizer (a new worker RPC);
  runtimes without it fail cleanly rather than returning a fabricated estimate.
- **Not implemented in v1** (returned as an explicit `invalid_request_error`,
  never silently ignored): Anthropic server-side tools, extended thinking with
  signed blocks, PDF/`document` content blocks, Anthropic-side MCP. `cache_control`
  is accepted and ignored; MLXBar never reports Anthropic cache-usage figures and
  never renames a Claude model id to a local one (an unmatched name routes to the
  sole resident model, and the response `model` field is the real local name).
- Feature flag `api.anthropic.enabled` (default on), latched at startup like
  `models.pool.enabled`.

## Compatibility / 互換性

- `openai_compat.py` unchanged; `/v1/models` routing, `/v1/chat/completions`,
  128-tool ceiling, bearer auth, SSE keep-alive comments and the OpenAI error
  JSON shape are all unchanged. Settings schema stays version 1; v1.7.x configs
  load unchanged via deep merge.
- `replicas == 1` keeps the pool byte-identical to v1.7.1, including the
  `_slots` key, worker instance key, manifest/log/socket paths and every status
  field.

## Verification / 検証

- Python regression suite: **346 passed** (316 from v1.7.1 + 30 new).
- Swift Debug and Release builds: passed.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware parallel-generation, memory, and Claude Code checks: pending, see
  `TEST_PLAN_v1.8.0.md §2–3`.
- Design and invariants: `DESIGN_v1.8.0.md`. Maintenance notes: `mlx-bar.md`.
- Apple notarization: not done (ad-hoc signature).

## Checksum

`2745bf80caad13dac9e972276abdb00056e743aece8490b335fac5bba0b361f2`  `MLXBar-1.8.0.dmg`

(Hash of the `dist/MLXBar-1.8.0.dmg` produced by `build-release.sh` on 2026-08-30;
also in `dist/MLXBar-1.8.0.dmg.sha256`. DMG packaging is not bit-reproducible —
re-hash if rebuilt.)
