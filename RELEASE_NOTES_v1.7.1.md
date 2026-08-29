# MLXBar v1.7.1

Bug-fix and interoperability release on top of v1.7.0. No changes to the model
pool's runtime behavior: `generationConcurrency=1` stays byte-equivalent to
v1.6.2 and `models.pool.enabled=false` stays byte-equivalent to v1.6.1.

## All resident models show in the menu bar / 常駐モデルをメニューバーに一覧表示

The model pool has held several models at once since v1.6.2, and
`GET /api/v1/status` has reported every one of them in `loadedModels` since then,
but the menu-bar popover only ever drew the single primary model. It now lists
every resident model, each row showing the engine, the reserved memory, a
keep-resident (pin) toggle, and a per-row unload button. The header still shows
the most recently used model, and "Unload All" is unchanged.

v1.6.2 以降プールは複数モデルを常駐でき、`GET /api/v1/status` の `loadedModels`
にも全モデルが載っていましたが、メニューバーは代表1件しか描画していませんでした。
v1.7.1 では常駐している全モデルを行として表示し、各行にエンジン・メモリ予約量・
常駐維持（ピン）切り替え・その行だけのアンロードを置きます。ヘッダーと
「すべてアンロード」の挙動は不変で、単一常駐時の見た目も変わりません。

## Errors read in the configured language / エラーが設定した言語で表示される

Several paths surfaced English to a user who had chosen Japanese: the management
API's internal-error handler returned a bare exception class name, request
validation returned pydantic's English text, many routes sent only a machine
`code` with no message, and upstream runtime exceptions (mlx-lm / mlx-vlm /
transformers) were passed through verbatim.

v1.7.1 keeps the coordinator and workers emitting a stable machine `code` plus a
Japanese `message` on every `MLXBarError`; the GUI resolves the `code` to text in
the chosen interface language via the new
`Sources/MLXBar/Services/ErrorText.swift`, and falls back to the server `message`
then the `code` for anything unknown. Upstream English exception text is replaced
with a classified Japanese sentence and kept in a `detail` field for the log.
Management-API internal errors, validation errors, and every previously
code-only response now carry a Japanese `message`.

**The OpenAI-compatible error body shape and `code` values are unchanged** —
external API consumers still receive `code` + `message`; only the language of
`message` changed.

## OpenAI-compatible clients: Zed / Cline / OpenCode

- **`max_tokens` default.** When a request sends neither `max_tokens` nor
  `max_completion_tokens`, MLXBar now applies the configured API ceiling
  (`generation.maxTokens`, default 8192, capped at the model's own limit) instead
  of a fixed 512. Agent-style clients routinely omit both fields and were being
  truncated at 512. Explicit values are still clamped to the ceiling exactly as
  before. Documented in `mlx-bar.md`.
- **Single-resident routing.** With exactly one model resident and
  `models.autoLoadOnAPIRequest` enabled, a request whose `model` name matches no
  resident is routed to that sole resident instead of trying to autoload a
  different model (which fails with `ENGINE_BUSY` while another model is
  generating). With two or more residents, resolution is unchanged.
- **Streaming tool-call `role`.** `delta.role: "assistant"` is now sent only on
  the first tool-call chunk, matching OpenAI, so strict SDK parsers (the Vercel
  AI SDK used by OpenCode) do not choke.
- **Unknown endpoints.** `POST /v1/completions` and any unknown path now answer
  in the OpenAI `error` shape (`code: UNSUPPORTED_ENDPOINT` / `HTTP_404`) rather
  than FastAPI's `{"detail": "Not Found"}`.

いずれも「OpenAI互換」プロバイダとして接続します。詳細は README「Zed / Cline /
OpenCode」節を参照してください。

## Compatibility / 互換性

- `models.pool` invariants unchanged; `generationConcurrency=1` ≡ v1.6.2,
  `enabled=false` ≡ v1.6.1 (byte-identical). OpenAI surface (`/v1/models`, Chat
  Completions routing, 128-tool ceiling, bearer auth, SSE keep-alive comments)
  and settings schema version 1 are unchanged.
- API error responses still include both `code` and `message`.

## Verification / 検証

- Python regression suite: **316 passed** (307 from v1.7.0 + 9 new). New tests
  cover the omitted-`max_tokens` fallback, explicit `max_tokens`, the
  `/v1/completions` and unknown-path OpenAI error shape, the single streaming
  tool-call `role`, single-resident routing, and non-empty `message` on
  management and OpenAI errors.
- Swift Debug and Release builds: passed.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed;
  `codesign --verify --deep --strict` clean, `hdiutil verify` clean, no
  `__pycache__` in the bundle). The packaged Coordinator reports
  `MLXBarCoordinator` with `--home` only, matching the entry point.
- On-hardware GUI and inference checks: pending, see `TEST_PLAN_v1.7.1.md §2–4`.
- Apple notarization: not done (ad-hoc signature; no Developer ID set — see
  `mlx-bar.md` "ad-hoc署名とlaunchdラベル競合").

## Checksum

`743c0b2d242884b75ab5f1aca491ce72ab58fcd743547543265d6b8513e409f6`  `MLXBar-1.7.1.dmg`

(Hash of the `dist/MLXBar-1.7.1.dmg` produced by `build-release.sh` on 2026-08-29;
also in `dist/MLXBar-1.7.1.dmg.sha256`. DMG packaging is not bit-reproducible —
re-hash if rebuilt.)
