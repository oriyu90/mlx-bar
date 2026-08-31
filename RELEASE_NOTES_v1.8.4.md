# MLXBar v1.8.4

A bugfix release: a reasoning model's `<think>` block no longer leaks into
`content` when the request carries no `tools`. No endpoint handler changes, no
settings-schema changes.

## Fix: chain-of-thought leaked into `content` on tool-less requests / tool無しリクエストで推論ブロックが本文へ漏れる不具合を修正

A thinking-capable model queried **without** a `tools` array returned the
contents of its `<think>…</think>` block verbatim in `content`. For a chat
template that pre-opens `<think>` in the prompt (the Ornith 1.5 family), the
output carried a bare closing `</think>` with the reasoning prose in front of
it. Ornith 1.5 makes this bite where Qwen doesn't: it ignores every
thinking-disable knob (`enable_thinking=false`, `/no_think`,
`reasoning_effort=none`) and always produces a reasoning block, whereas Qwen
respects the template toggle and stays silent on a tool-less call.

Root cause: `Workers/common/server.py`'s `/generate` loop only built and used
the `<think>`-splitting `IncrementalToolStream` when `tool_mode` was on
(`bool(params.get("tools")) and tool_choice != "none"`). A request with no
`tools` went through a raw passthrough branch — reasoning text and the closing
tag streamed straight into `content`. The v1.2.0 strip
(`parse_tool_markup`'s `re.sub`) only runs inside `tool_mode` and only removes
a `<think>…</think>` block when *both* tags are present, so a pre-opened think
block left the prose behind.

Fixed by making the split independent of `tool_mode`:

- `IncrementalToolStream` gained a `reasoning_only` flag — it separates
  `<think>`/`</think>`/`<assistant>` and never treats `<tool_call>` markup as
  special. `server.py` now constructs it for every request (`reasoning_only`
  when there are no tools), and the `reasoning_start` handling and the final
  `finish()` flush moved out of the `if tool_mode:` guard.
- `mlx_lm_worker` and `mlx_vlm_worker` emit `reasoning_start` whenever the
  rendered prompt ends with `<think>`, with or without tools.

見つかった経緯: OpenClaw との実接続テストで、`Ornith-1.5-35B-A3B-MLX-4bit` をメイン
モデルに設定した際に生の思考文が配信チャネルへ流出。LM Studio 直・mlx-bar（`tools` あり）
は正常、mlx-bar（`tools` なし）でのみ再現、という切り分けから根本原因を特定。詳細は
`DESIGN_v1.8.4.md` と `mlx-bar.md` の「tool無しリクエストで推論ブロックが本文へ漏れる（v1.8.4対応）」。

## Compatibility / 互換性

- `tools`-bearing requests are logically unchanged:
  `IncrementalToolStream(reasoning_only=False)` is the pre-1.8.4
  `IncrementalToolStream()`. Every tool-path test passes unmodified.
- A tool-less response that spontaneously emits `<tool_call>` markup keeps
  streaming it as visible content, exactly as before — no new
  `TOOL_PARSE_FAILED` path for tool-less requests.
- A non-reasoning model streams byte-for-byte as before, apart from at most
  ~11 characters held back for one delta when a chunk ends on a partial
  `<…` marker prefix (flushed on the next delta / at `finish()`; no content
  is lost).
- Coordinator (OpenAI-compatible and Anthropic-compatible, streaming and
  non-streaming), management API, and settings schema (version 1): no changes.
  `reasoning_delta` already maps to `reasoning_content` on the OpenAI stream,
  and is already dropped on the OpenAI non-stream and both Anthropic paths.

### Intended behavior change / 意図した挙動変更

Querying a reasoning model **without** `tools` now routes `<think>…</think>`
to `reasoning_content` (streaming) / drops it (non-streaming) instead of
placing it in `content`. A client that was reading chain-of-thought out of
`content` on tool-less requests will stop seeing it there.

## Verification / 検証

- Python regression suite: **373 passed** (367 from v1.8.3 + 6 new: 3
  streaming-separation integration tests for the tool-less path, 1 regression
  guard for non-reasoning models, 1 for spontaneous tool markup, 1
  `IncrementalToolStream(reasoning_only=True)` unit test). The four
  fix-dependent tests were confirmed to fail against the pre-fix `Workers/`
  code and pass against the fix; the two guard tests pass both ways.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed).
- On-hardware confirmation (2026-08-31): tool-less OpenAI-compatible requests
  to the installed `/Applications/MLXBar.app` v1.8.4 build against
  `Ornith-1.5-35B-A3B-MLX-4bit` — `content` clean (no `</think>`, no reasoning
  prose) on both streaming and non-streaming; `tools`-bearing requests
  unchanged from v1.8.3.
- Design and invariants: `DESIGN_v1.8.4.md`. Maintenance notes: `mlx-bar.md`.
- Apple notarization: not done (ad-hoc signature).

## Checksum

`3299176be97278a6d7d80b0a714ecdbaf3834ec3524ae758b89591f7e9a03a09`  `MLXBar-1.8.4.dmg`

(Hash of the `dist/MLXBar-1.8.4.dmg` produced by `build-release.sh` on
2026-08-31; also in `dist/MLXBar-1.8.4.dmg.sha256`. DMG packaging is not
bit-reproducible — re-hash if rebuilt.)
