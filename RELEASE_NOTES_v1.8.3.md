# MLXBar v1.8.3

A bugfix release: tool-calling with the Ornith 1.5 family no longer crashes on
the mlx-lm engine. No other endpoint behavior changes, no settings-schema
changes.

## Fix: Ornith 1.5 crashed on every tool-calling request / Ornith 1.5系がtool呼び出しで必ずクラッシュしていた不具合を修正

Any conversation containing a `tool`-role message (the second turn of a
tool-calling exchange) reproducibly crashed with `GENERATION_FAILED` on both
`Ornith-1.5-35B-A3B-MLX-4bit` and `Ornith-1.5-9B-MLX-8bit`, whenever the model
was served through the mlx-lm engine. The first turn (no tool messages yet)
always worked fine, which made this easy to miss in casual testing and easy to
hit in any real agent loop.

Root cause: the OpenAI-compatible wire format keeps
`assistant.tool_calls[].function.arguments` as a JSON-encoded *string* (per
spec). `mlx_lm_worker` passed that string straight through to the tokenizer's
`apply_chat_template`. Ornith 1.5's chat template renders each tool call's
arguments with Jinja's `|items` filter, which needs an actual mapping and
raises `TypeError: Can only get item pairs from a mapping.` on a string.
`mlx_vlm_worker` never hit this — `mlx_vlm.prompt_utils.apply_chat_template`
parses that string into a dict first via its own private
`_normalize_tool_message`, before this project's code ever sees it.

Fixed by adding the equivalent transform, `normalize_tool_call_messages()`, as
this project's own shared code in `common/tool_calls.py` (rather than reaching
into a third-party private API), and applying it in `mlx_lm_worker`'s
`stream()` and `_render_prompt()` before template rendering. A conversation
with no tool calls is untouched — literally the same list object passed
through, no copy.

見つかった経緯: OpenClaw（外部クライアント、tool呼び出しを行うsub-agentのモデルとして
Ornith-1.5-9B-MLX-8bitを検証中）との実接続テストで発見。実際にクラッシュしたリクエストを
プロキシで捕獲し、開発用チェックアウトから本番と同一のCoordinator＋Worker一式を起動して
再現・トレースバック取得まで行った。詳細は`DESIGN_v1.8.3.md`と`mlx-bar.md`§0-Uを参照。

## Compatibility / 互換性

- Management API, OpenAI-compatible surface and Anthropic-compatible surface: no
  handler changes beyond the two `mlx_lm_worker` call sites and the new shared
  helper. `mlx_vlm_worker` is completely untouched.
- A conversation with no tool calls renders through the exact same code path as
  before, on the exact same object — no behavior change, no new copy.
- A client that already sends `arguments` as a parsed dict (rather than the
  OpenAI-spec string) is unaffected: the transform only fires on strings.
- Settings schema stays version 1; existing configs load unchanged.

## Verification / 検証

- Python regression suite: **367 passed** (361 from v1.8.2 + 6 new: 4 unit
  tests for `normalize_tool_call_messages`, 2 integration tests for
  `MLXLMAdapter.stream()`). The crash-reproduction test was confirmed to fail
  against the pre-fix code and pass against the fix.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware confirmation (2026-08-31): replayed the exact request that used
  to crash 100% of the time, through the installed `/Applications/MLXBar.app`
  build, against both `Ornith-1.5-9B-MLX-8bit` (5 consecutive runs, all
  succeeded) and `Ornith-1.5-35B-A3B-MLX-4bit` (succeeded).
- Design and invariants: `DESIGN_v1.8.3.md`. Maintenance notes: `mlx-bar.md`.
- Apple notarization: not done (ad-hoc signature).

## Known issue found during this verification (separate from this fix)

Ornith 1.5 occasionally emits a tool-call in a shape the runtime's own parser
can't decode, surfacing as `TOOL_PARSE_FAILED` (a different, pre-existing
error code, not `GENERATION_FAILED`). This looks like a model output-quality
issue rather than an mlx-bar bug; not investigated further here.

## Checksum

`f1c680a5a618235e8049afd33c7032f2ba92c784eac17304ee9c863daba30285`  `MLXBar-1.8.3.dmg`

(Hash of the `dist/MLXBar-1.8.3.dmg` produced by `build-release.sh` on
2026-08-31; also in `dist/MLXBar-1.8.3.dmg.sha256`. DMG packaging is not
bit-reproducible — re-hash if rebuilt.)
