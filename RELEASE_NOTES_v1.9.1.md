# MLXBar v1.9.1

A bugfix release addressing four findings from a 2026-09-02 code audit of the
OpenAI/Anthropic compatibility layer. No endpoint/route changes, no
settings-schema changes, no Swift GUI changes.

バグ修正のみのパッチリリース。2026-09-02 のコード監査で報告された4件を、互換性・
既存機能・クラッシュ安全性・メモリ安全性を保ったまま修正しました。

## Fix 1: streaming repeats `delta.role` when a response has 2+ tool calls / マルチtool callで`role`が繰り返される

`_tool_call_stream_chunks` (`Coordinator/mlxbar/api/openai_compat.py`) emitted
`{"role": "assistant", ...}` on the opening chunk of **every** tool call. A
model that produces two `<tool_call>` blocks in one response therefore streamed
`delta.role` N times — the exact breakage the L266-270 comment and the
`tool_call_role_sent` guard exist to prevent for strict SSE parsers (Vercel AI
SDK / OpenCode). The guard only covered the separate `tool_call_delta` event
path. Fixed by attaching `role` only to the first tool call (`index == 0`).
Single-tool-call output, the `tool_call_delta` path, and the non-stream path are
unchanged.

## Fix 2: tool-less non-streaming requests silently dropped a reasoning model's `<think>` content / `tools`無し非ストリームで推論内容がどこにも返らず捨てられていた

Since v1.8.4 the worker's `IncrementalToolStream` runs on tool-less requests
too, so a reasoning model's `<think>…</think>` reaches the coordinator as a
`reasoning_delta` event. The streaming path forwards it as
`delta.reasoning_content`; the **non-streaming** `/v1/chat/completions` loop had
no branch for it, so the reasoning was placed nowhere — not in `content` (the
v1.8.4 intent) and not returned as `reasoning_content` either. Silent data loss.

Fixed by accumulating `reasoning_delta` on the non-stream path and returning it
as `message.reasoning_content` when non-empty. **`content` is never touched** —
the v1.8.4 invariant (raw chain-of-thought must not reach a delivery channel,
per the 2026-08-31 Telegram incident) holds. `message.reasoning_content` is
additive and ignored by OpenAI clients (the DeepSeek/vLLM convention). This
intentionally revises v1.8.4's "do not add `reasoning_content` to the non-stream
response" non-goal: not leaking and discarding are different problems, and the
streaming/non-streaming asymmetry was unpredictable for clients. Anthropic paths
are unchanged (v1 does not surface unsigned thinking blocks).

## Fix 3: `int(delta.get("index", …))` crashes mid-stream on `"index": null` / `index: null` で応答途中にクラッシュ

`_merge_tool_call_deltas` (`openai_compat.py`) and
`AnthropicMessageBuilder._merge_tool_call_delta` (`anthropic_stream.py`) called
`int()` on `delta["index"]` when the key was present. A JSON `null` (or any
non-integer) made `int(None)` raise `TypeError` inside the `async for`, taking
the response stream down. Both now fall back to the delta's positional index for
any non-integer value — the same meaning as an absent `index`. Behavior for
integer indices is unchanged; mlx-bar's own workers always emit integers, so
this is defense against malformed/future upstream event shapes.

## Fix 4: parenthesize a mixed `or`/`and` load-failure classifier / 非括弧の`or`/`and`式に括弧を追加

`Workers/common/server.py`'s `rpc()` load-exception classifier wrote
`... or "metal" in lowered and "memory" in lowered:` unparenthesized. Operator
precedence already made it correct and equivalent to its parenthesized twin in
the `/generate` handler (L610-611); this only adds the parentheses so the two
read the same. **No behavior change.**

## Compatibility / 互換性

- Single-tool-call streaming output is byte-identical to v1.9.0.
- `content` still carries only `delta`-event text on every path.
- Integer `index` merge behavior is unchanged.
- Worker generation logic is unchanged apart from Fix 4's parentheses (stop
  sequences, heartbeats, `tool_mode`, `finalize`, memory watchdog, token-rate
  all untouched).
- Coordinator routes, settings schema (version 1), management API, and the
  Swift GUI: no changes. No new localized strings.

### Intended behavior changes / 意図した挙動変更

- A reasoning model queried **without** `tools` in **non-streaming** mode now
  returns `message.reasoning_content` (when a `<think>` block was produced);
  previously that content was discarded. `content` stays clean.
- In a stream with 2+ tool calls, the opening chunk of the 2nd and later tool
  calls no longer carries `delta.role` (matches OpenAI's wire format). Single
  tool call unchanged.

## Verification / 検証

- Python regression suite: **380 passed** (376 from v1.9.0 + 4 new): multi
  tool-call role-once, `_merge_tool_call_deltas` null-index tolerance,
  non-stream reasoning returned as `reasoning_content` and kept out of
  `content`, `AnthropicMessageBuilder` null-index tolerance. The four new tests
  were confirmed to fail against pre-fix `Coordinator/`/`Workers/` code and pass
  against the fix. Run 3× consecutively, stable.
- Known deferred flake `test_buffered_tool_generation_still_emits_heartbeats`:
  unchanged, still deferred (observability test, unrelated to compatibility;
  recorded in `mlx-bar.md`).
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware confirmation: tool-less OpenAI-compatible requests to the
  installed `/Applications/MLXBar.app` v1.9.1 build — non-stream returns
  `reasoning_content`, `content` clean; streaming unchanged; `tools`-bearing
  path unchanged; `/api/v1/health` reports `1.9.1`.
- Design and invariants: `DESIGN_v1.9.1.md`. Test plan: `TEST_PLAN_v1.9.1.md`.
  Maintenance notes: `mlx-bar.md`.
- Apple notarization: not done (ad-hoc signature).

## Checksum

`4378e97f4f748a5879df3793c80b74d1ebd2dbba3175885dd270b32c05c43f94`  `MLXBar-1.9.1.dmg`

(Hash of the `dist/MLXBar-1.9.1.dmg` produced by `build-release.sh`; also in
`dist/MLXBar-1.9.1.dmg.sha256`. DMG packaging is not bit-reproducible — re-hash
if rebuilt.)
