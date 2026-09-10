# MLXBar v2.2.0

An experimental **Adaptive Hybrid Context Memory**: older conversation turns are
routed, per segment, to EXACT (kept verbatim), LATENT (a short cached summary
note, optionally further compressed to soft tokens and hybrid-prefilled by the
mlx-lm worker) or COLD (dropped from inference, recoverable from the raw message
the stateless client re-sends). **Off by default** -- a request behaves exactly
as in v2.1.0 unless `experimental.adaptiveMemory.enabled` is set. Every failure
falls back to normal EXACT inference and never becomes an API error. It cannot be
used together with Automatic Context Compression.

セグメント単位で古い会話ターンを EXACT（逐語）／LATENT（短い要約ノート、任意で mlx-lm
Worker がソフトトークンへ圧縮し Hybrid Prefill）／COLD（推論から除外、元メッセージから
復元）へ振り分ける実験的機能。**既定で無効**で、`experimental.adaptiveMemory.enabled` を
設定しない限り挙動は v2.1.0 と同一。あらゆる失敗は通常の EXACT 推論へ自動フォールバックし、
API エラーにはしない。コンテキスト自動圧縮とは併用不可。

## Added: coordinator tier (`Coordinator/mlxbar/api/adaptive_memory/`)

Always the safe tier -- it only ever rewrites the `messages` list, with the same
best-effort, never-raises contract as `context_compression.py`.

- **`segment.py`** -- Context Structure Analyzer: per-message verbatim-risk
  classification (code fences, diff/patch, shell, JSON/XML, file paths,
  hash/UUID, numeric density, tool traffic, images, system/developer).
- **`policy.py`** -- deterministic Adaptive Policy v1. Presets `fidelity` /
  `balanced` / `memorySaver` tune how far back a LATENT candidate survives before
  it is dropped to COLD. `POLICY_VERSION = "am1"`.
- **`planner.py`** -- reuses `context_compression`'s tail splitter, size helper
  and summarizer. COLD turns are dropped, contiguous LATENT runs become one
  `[Earlier discussion condensed]` system note, EXACT turns and the verbatim tail
  pass through. Returns the input unchanged if the rebuild is not at least 5%
  smaller.
- **`latent_store.py`** -- content-addressed note cache
  (`SHA256(canonical message + role + model fingerprint + POLICY_VERSION)`). A
  re-sent identical history skips the summarizer. Corruption / truncation -> miss.
- **`metrics.py`** -- `AdaptiveMetrics` (raw/effective chars, exact/latent/cold
  counts, `compression_ratio`, `policy_ms` / `writer_ms`, cache hit/miss,
  `fallback_reason`).

## Added: mlx-lm worker soft-token tier (`Workers/mlx_lm_worker/adaptive_memory/`)

Active only when `experimental.adaptiveMemory.softToken` is on **and** the loaded
reader passes capability probes. **Fails closed**: `AdaptiveRuntime.prepare`
raises `FallbackToExact` -- and nothing else -- on any doubt, and the adapter
catches it before a single token is yielded, so it can never break a request or
crash the worker.

- **Writer `meanpool-v1`** -- text tokens -> soft tokens with no training: embed
  the older band through the model's own input embedding layer, then mean-pool
  contiguous windows to at most `maxLatentTokens`. Deterministic, parameter-free.
  Quality is not claimed -- this is a mechanism to measure on Apple Silicon,
  which is why `softToken` ships OFF.
- **`hybrid_prefill.py`** -- prefills the EXACT head and the LATENT soft-token
  band into one KV cache in order (`model(placeholder, cache, input_embeddings=…)`
  for the band), then hands the cache + verbatim tail to `stream_generate`.
  `input_embeddings` unsupported -> fallback.
- **`latent_store.py` / `hybrid_cache.py`** -- content-addressed soft-token cache
  (`.npy` + RAM LRU); the Hybrid KV key composition is defined
  (reader/writer/template/policy/mlx-lm version + block hashes) but reuse is a
  future version (v1 always does a fresh prefill).

## Added: request integration, status, CLI, GUI

- `maybe_plan_adaptive_memory` is called once in `openai_compat.chat()` and
  `anthropic_compat._messages()`, right after `maybe_compress_messages`; the
  worker-tier knobs are merged into the generation `options`. `/api/v1/status`
  gains an `adaptiveMemory` summary (`compression_ratio`, `fallback_reason`);
  `AppState.last_adaptive_memory` mirrors `last_context_compression`.
- `mlxbarctl config set-adaptive-memory --…` (only the options you pass change),
  mirroring the GUI; the generic `config set experimental.adaptiveMemory.<key>`
  still works.
- **Settings > Advanced > Experimental** (Japanese and English): master toggle,
  policy picker, steppers (trigger %, memory-pressure %, recent turns kept EXACT,
  max soft tokens, max retrieved segments), toggles (verbatim protection,
  soft-token Hybrid Prefill), Apply, and a caption. The menu bar shows the most
  recent ratio or fallback reason.

## Compatibility / 互換性

- `experimental.adaptiveMemory.enabled = false` (default) -> fully dormant; an
  ordinary request never reaches the package and no cache directory is created.
- Mutually exclusive with `contextCompression`: enabling both -> HTTP 422.
- Settings schema is additive; `schemaVersion` stays 1. An existing `config.json`
  needs no migration. The new `/api/v1/status` `adaptiveMemory` key is additive;
  an older GUI ignores it.
- `Workers/mlx_lm_worker/adapter.py` OFF path is byte-identical: the branch is
  gated on `enabled` **and** `softToken`, and the EXACT prompt cache
  fetch/store is untouched when adaptive is inactive. mlx-vlm worker: unchanged.
- Worker / Coordinator<->Worker RPC / prompt cache / model pool / RAG / the
  OpenAI and Anthropic wire formats (with no `adaptiveMemory`): unchanged.

## Signing / 署名

Ad-hoc signed (`codesign --sign -`), **not** notarized -- no Apple Developer
Program membership. On first launch, approve it from System Settings -> Privacy &
Security. ad-hoc 署名のみ・未公証です。初回起動時は「システム設定 → プライバシーと
セキュリティ」から起動を許可してください。

## Verification / 検証

- Python regression suite: **466 passed** (430 from v2.1.0 + 34 new in
  `test_adaptive_memory.py` + 2 new in `test_cli.py`). Stable in default and
  fixed order. MLX is never imported by the tests (fakes only).
- `swift build --disable-sandbox`: succeeds. `plutil -lint` on the strings file:
  OK. Every new Japanese UI key has an English translation.
- Design and invariants: `DESIGN_v2.2.0.md`. Test plan and on-hardware checks:
  `TEST_PLAN_v2.2.0.md`.

## Checksum

`814612bac0a1bb3b0a1293ba9a2c0145acce731cf614212c1eda972168e4137c`  `MLXBar-2.2.0.dmg`
