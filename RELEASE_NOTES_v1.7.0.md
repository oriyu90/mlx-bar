# MLXBar v1.7.0

## Different models generate at the same time / 別々のモデルが同時に生成

Distinct resident models can now generate concurrently, up to `models.pool.generationConcurrency`
(default 2, range 1–8). Requests to the *same* model stay serialized in arrival order, because one
MLX process is single-threaded. Setting `generationConcurrency` to 1 restores the pre-v1.7.0
behavior of one generation at a time across the whole pool (order, queue, cancel, `/api/v1/status`);
the only difference is that a `queue` event's `position` is now counted per model rather than
globally. The `enabled=false` path is byte-identical to v1.6.1.

v1.6.2 まではプール全体で生成を1件に直列化していました。v1.7.0 は、プロセス分離済みで個別に
admission 済みの常駐モデルに限り、メモリ・ヘッドルームガードと macOS メモリ圧の監視の下で
モデル**間**の並行生成を許可します。同一モデルへの複数要求は従来どおり到着順に直列です。
`generationConcurrency=1` で v1.6.2 と挙動がバイト等価です。

### Memory head-room guard / メモリガード

The first generation is always allowed. A second or later concurrent lane starts only when the
resident reservations plus a per-generation head-room charge still fit the pool budget, and macOS
reports no memory pressure. Otherwise the request is queued rather than failed, and it runs as soon
as a lane frees. Under warning/critical pressure the pool falls back to serial automatically.
`models.pool.perGenerationHeadroomGB` (default 0 = derive `min(perModelLimit × 0.15, 2 GiB)`,
range 0.25–32) overrides the charge.

1件目の生成は常に許可します。2レーン目以降は、常駐予約＋生成ヘッドルームが予算内で、かつ
macOS がメモリ圧を報告していないときだけ開始します。通らない要求は失敗させずキューへ回し、
レーンが空き次第実行します。逼迫時は自動的に直列へ降格します。

### Per-model queue, cancel, and orphan recovery / モデル別のキュー・キャンセル・孤児回復

Each model has its own lane: queue position, wait time, and cancellation are computed per model.
An orphaned lane (a client that disconnected mid-handshake) is recovered with the same predicate
`WorkerSupervisor` uses, now per model — v1.6.2's ad-hoc pool queue had no equivalent.

### Preload / multi-model UX

- `POST /api/v1/models/{id}/unload` (`?force=`) frees one resident model and leaves the rest
  loaded. `DELETE /api/v1/models/loaded` (unload all) is unchanged.
- `mlxbarctl`: `model unload <id>`, `model resident`, `model pin <id>`, `model unpin <id>`.
- Settings shows a `generationConcurrency` stepper, a "models to keep resident" toggle list
  (`models.pool.profiles`), and a live "concurrent generations N / limit M" readout.
- Boot preload of `keepLoaded` profiles (`_preload_last_model`) is unchanged in behavior.

### Compatibility / 互換性

- `enabled=false` still delegates entirely to the v1.6.1 single-worker path.
- OpenAI-compatible surface unchanged: `/v1/models`, Chat Completions routing, the 128-tool
  ceiling, bearer auth, SSE keep-alive comments.
- Settings schema stays version 1; v1.6.x configs load unchanged via deep merge.

### Default choice / 既定値について

`generationConcurrency` ships defaulting to 2 at the owner's explicit request. This diverges from
the "don't move a default without a measurement" guideline in `common rules` / `mlx-bar.md`: the
combined multi-model allocation peak has **not** been measured on hardware in this release. The
memory guard keeps a request that would have succeeded serially from failing, and drops to serial
under pressure. `TEST_PLAN_v1.7.0.md §2` is the on-hardware procedure that must be run before this
default is considered validated.

## Verification / 検証

- Python regression suite: 307 passed (289 from v1.6.2 + 18 new).
- Swift Debug and Release builds: passed.
- `build-release.sh` + `verify-release.sh`: passed (ad-hoc signed; `codesign --verify --deep
  --strict` and `hdiutil verify` clean). The packaged Coordinator reports `version 1.7.0`, and a
  smoke test against the built binary confirmed the new `modelPool` status fields, the
  `generationConcurrency` / `perGenerationHeadroomGB` validation, the `POST
  /api/v1/models/{id}/unload` route, bearer auth, and the 128/129-tool boundary. Details in
  `TEST_PLAN_v1.7.0.md §1` and `§4`.
- On-hardware concurrent-generation and OpenClaw checks: pending, see `TEST_PLAN_v1.7.0.md §2–3`.
- Invariants and the v1.6.2 non-goal update: `DESIGN_v1.7.0.md`.
- Apple notarization: not done (ad-hoc signature; see `mlx-bar.md §5`).

## Checksum

`4bef7d701b644918d7536d64b2db0e6d02db56f3a943025faa82e2f51f520ca3`  `MLXBar-1.7.0.dmg`

(Hash of the `dist/MLXBar-1.7.0.dmg` produced by `build-release.sh` on 2026-08-28; also in
`dist/MLXBar-1.7.0.dmg.sha256`. DMG packaging is not bit-reproducible — re-hash if rebuilt.)
