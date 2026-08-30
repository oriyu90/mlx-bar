# MLXBar v1.8.1

A maintenance release on top of v1.8.0: every GUI operation is now reachable from
`mlxbarctl`, plus three small fixes carried over from the v1.8.0 review. No
endpoint behavior changes, no settings-schema changes. The additions to
`mlxbarctl` are purely new subcommands — existing commands are unchanged.

## Full CLI coverage of GUI operations / GUI 操作の CLI 完全対応

The GUI and `mlxbarctl` both drive the same management API. These GUI actions had
no named CLI command before v1.8.1:

| GUI action | New command |
|---|---|
| Clear prompt cache (memory / disk) | `mlxbarctl prompt-cache clear-memory` / `clear-disk` |
| Prompt-cache stats | `mlxbarctl prompt-cache status` |
| Persistent prompt-cache settings | `mlxbarctl prompt-cache set --disk-enabled {true,false} --max-gb N` |
| Multi-model residency form (7 fields) | `mlxbarctl config set-model-pool --enabled … --max-resident … --idle-ttl-seconds … --per-model-gb … --total-memory-percent … --system-reserve-gb … --generation-concurrency … --max-replicas-per-model …` |
| Change replicas of an already-pinned model (no reload) | `mlxbarctl model set-replicas <id> <count>` |
| Force the all-models unload | `mlxbarctl model unload --force` (previously the flag was ignored on the no-argument path) |
| GUI toggles | `mlxbarctl config set-flag <name> {true,false}` — `auto-load-on-api`, `anthropic-api`, `remote-image-urls`, `require-token`, `continue-after-gui-exit` |
| LM Studio Base URL / auto-load | `mlxbarctl lmstudio set-base-url <url>` / `mlxbarctl lmstudio set-auto-load {true,false}` |

`config set-model-pool` and `prompt-cache set` send only the options you pass, so
`models.pool.profiles` and the other cache keys are left untouched. The generic
`mlxbarctl config set <dotted.key> <json-value>` still works as an escape hatch
for settings the GUI does not expose.

Client-side range checks match the GUI; the coordinator's own `_validate` remains
the authority. `SMAppService`/Login Items registration stays GUI-only (documented
since v1.7.1): `mlxbarctl config set-launch-at-login` changes the setting and the
GUI reconciles it on next launch.

## Fixes carried over from the v1.8.0 review

- **Anthropic streaming input-token count.** `message_delta.usage` now carries the
  real `input_tokens` (the worker's tokenizer count), not just the pre-generation
  estimate that `message_start` reports. The builder now also reads `prompt_tokens`
  from `metrics` events, so runtimes that do not emit a separate `usage` event
  (mlx_vlm, the mlx_lm fallback path) still report the real count.
- **`count_tokens` unavailable → `api_error`.** When a runtime cannot count
  tokens the endpoint returns HTTP 503 with `error.type: "api_error"` (was
  `invalid_request_error`), matching the status.
- **GUI hardening.** The per-model replicas stepper builds its range as
  `1...max(1, maxReplicasPerModel)`, and the pinned-profile map is built with
  `Dictionary(_:uniquingKeysWith:)`, so a hand-corrupted settings file cannot
  crash the settings screen.

## Compatibility / 互換性

- Management API, OpenAI-compatible surface and Anthropic-compatible surface: no
  handler changes. `/v1/models` routing, `/v1/chat/completions`, the 128-tool
  ceiling, bearer auth, SSE keep-alive comments and both error JSON shapes are
  unchanged.
- Settings schema stays version 1; v1.7.x / v1.8.0 configs load unchanged.
- `mlxbarctl` existing commands are unchanged. `model unload` with no flag still
  calls `DELETE /api/v1/models/loaded`.
- `replicas == 1` / `generationConcurrency == 1` / `models.pool.enabled == false`
  invariants from v1.8.0 are untouched.

## Verification / 検証

- Python regression suite: **360 passed** (346 from v1.8.0 + 14 new: 12 CLI, 2 Anthropic).
- Swift Debug and Release builds: passed.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware parallel-generation, memory and Claude Code checks: still pending
  from v1.8.0, see `TEST_PLAN_v1.8.0.md §2–3` — unaffected by this release.
- Design and invariants: `DESIGN_v1.8.1.md`. Maintenance notes: `mlx-bar.md`.
- Apple notarization: not done (ad-hoc signature).

## Checksum

`d0db97675858e5f8ab93e6821e33f08b2b53657aaf990b35b0196dfeccc93829`  `MLXBar-1.8.1.dmg`

(Hash of the `dist/MLXBar-1.8.1.dmg` produced by `build-release.sh` on 2026-08-30;
also in `dist/MLXBar-1.8.1.dmg.sha256`. DMG packaging is not bit-reproducible —
re-hash if rebuilt.)
