# MLXBar v1.8.2

A bugfix release: API-driven requests can once again grow the model pool past
one resident. No endpoint behavior changes beyond the fix itself, no
settings-schema changes.

## Fix: switching to a second model via the API silently did nothing / API経由の2体目モデルへの切り替えが無反応だった不具合を修正

With exactly one model resident, requesting a different model by name did not
load it and did not error — the coordinator silently kept answering with the
already-loaded model instead, no matter how high `models.pool.maxResidentModels`
was configured. This made the multi-model pool feature unreachable through
ordinary sequential API calls: you could always get from zero residents to one,
but never from one to two.

Root cause: `_ensure_requested_model`'s single-resident shortcut treated any
non-matching request name as an alias for the sole resident model, to avoid
failing single-model callers with `ENGINE_BUSY` on a generation-in-progress
model. The shortcut did not check whether the pool could actually hold a
second resident, so it fired unconditionally whenever exactly one model was
loaded — even when the requested name was a real, distinct, catalog-known
model and the pool had room for it.

Fixed to only take the alias shortcut when the pool is effectively
single-model (`maxResidentModels <= 1`) or the requested name doesn't match any
catalog model. A request for a real, distinct model now falls through to the
normal autoload / `ENGINE_BUSY` path, as originally intended.

見つかった経緯: OpenClaw（外部クライアント）との実接続テストで、メインモデルとサブエージェント
モデルを別々に`mlx-bar`へ割り当てたところ、2体目が一切ロードされずメインモデルが答え続ける現象を
発見。原因と修正の詳細は`DESIGN_v1.8.2.md`を参照。

## Compatibility / 互換性

- Management API, OpenAI-compatible surface and Anthropic-compatible surface: no
  handler changes beyond `_ensure_requested_model`. `/v1/models` routing, the
  128-tool ceiling, bearer auth, SSE keep-alive comments and both error JSON
  shapes are unchanged.
- Single-model operation (`maxResidentModels <= 1`) is bit-for-bit unchanged:
  the alias shortcut still applies exactly as before in that configuration.
- Settings schema stays version 1; existing configs load unchanged.

## Verification / 検証

- Python regression suite: **361 passed** (360 from v1.8.1 + 1 new regression
  test, `test_a_second_distinct_model_actually_loads_when_the_pool_allows_it`,
  confirmed to fail against the pre-fix code and pass against the fix).
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware confirmation (2026-08-30): from a cold state, sequential requests
  for two distinct real models (`Qwen3.8-27B-MLX-6bit` then
  `Ornith-1.5-9B-MLX-8bit`) both loaded correctly and stayed resident together;
  re-requesting the first model afterward still answered correctly.
- Design and invariants: `DESIGN_v1.8.2.md`. Maintenance notes: `mlx-bar.md`.
- Apple notarization: not done (ad-hoc signature).

## Known issue found during this verification (separate from this fix)

Multi-turn tool-calling (a conversation containing a `tool`-role message)
reproducibly crashes with `GENERATION_FAILED` on both `Ornith-1.5-35B-A3B-MLX-4bit`
and `Ornith-1.5-9B-MLX-8bit` — first-turn (no tool messages yet) requests are
fine. `Qwen3.8-27B-MLX-6bit` handles the identical multi-turn tool payload
correctly, so this looks specific to the Ornith family's chat template /
worker handling of tool-role messages, not the model-pool fix in this release.
Not yet investigated further; tracked as a follow-up.

## Checksum

`13096bbb16ed2b82d265f8d443c4ab095c9414ae364720caf08a0e2a9dd7b631`  `MLXBar-1.8.2.dmg`

(Hash of the `dist/MLXBar-1.8.2.dmg` produced by `build-release.sh` on 2026-08-30;
also in `dist/MLXBar-1.8.2.dmg.sha256`. DMG packaging is not bit-reproducible —
re-hash if rebuilt.)
