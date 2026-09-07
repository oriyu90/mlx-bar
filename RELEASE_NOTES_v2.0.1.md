# MLXBar v2.0.1

A single bug fix in the multi-model residency pool, plus the diagnostics that make
the failure visible. No API, settings-schema or compatibility changes.

複数モデル常駐プールのバグ修正1件と、その不足を可視化する診断表示の追加です。API・
設定スキーマ・互換性に影響はありません。

## Fixed: `maxResidentModels` counted extra replicas as extra "models"

Pool admission (`ModelPoolSupervisor._admit`) compared `len(self._slots)` -- the
number of worker slots, replicas included -- against `maxResidentModels`. So with
`maxResidentModels = 1`, pinning a profile with `replicas = 2` always had its second
replica rejected with `MEMORY_BUDGET_EXCEEDED`, even with plenty of memory free.
The setting UI ("maximum resident models"), the `residentModelCount` status field
and the replicas feature all mean *distinct models*, so admission and the runtime
`_reap_once` reduction now count **distinct model ids**. Per-replica memory checks
(per-model MLX cap, global budget, OS memory pressure, `maxReplicasPerModel`) are
still applied to every replica -- memory safety is unchanged.

入場制御（`ModelPoolSupervisor._admit`）が `maxResidentModels` を `len(self._slots)`
（＝レプリカ込みのWorkerスロット数）と比較していました。そのため `maxResidentModels = 1`
のとき `replicas = 2` の固定プロファイルを置いても、メモリに余裕があっても2体目が必ず
`MEMORY_BUDGET_EXCEEDED` で拒否されていました。設定UI（「最大常駐モデル数」）・
`residentModelCount` ステータス・レプリカ機能はいずれも「異なるモデルの数」を意味するため、
入場制御と実行時縮退（`_reap_once`）を**ユニークなモデルID数**で数えるよう修正しました。
レプリカ単位のメモリ検査（モデル単位のMLX上限、全体予算、OSメモリ圧、`maxReplicasPerModel`）は
各レプリカに従来どおり適用しており、メモリ安全性は変わりません。

## Added: replica-shortfall and restart-pending diagnostics (read-only)

- `/api/v1/status` `modelPool` now carries `configuredGenerationConcurrency` and
  `effectiveGenerationConcurrency`; `restartRequired` is also true when a saved
  `generationConcurrency` has not yet been applied (it is latched at coordinator
  start, like `enabled`).
- Best-effort replicas that could not be admitted are reported in
  `modelPool.replicaShortfalls` and per row in `loadedModels[].desiredReplicaCount`
  / `readyReplicaCount` / `replicaShortfall` (stable code and a safe summary only --
  no token, path or request body).
- `mlxbarctl models resident` surfaces all of the above.
- Settings > Models shows a restart-pending note and a "Replica shortfall" row only
  when applicable, in Japanese and English. This is a read-only display, not a new
  mutation, so no new named CLI command is required.

- `/api/v1/status` の `modelPool` に `configuredGenerationConcurrency` /
  `effectiveGenerationConcurrency` を併記。保存済みの `generationConcurrency` が未反映の
  場合は `restartRequired` を true にします。
- ベストエフォートで見送られたレプリカを `modelPool.replicaShortfalls` と各
  `loadedModels[]` 行の `desiredReplicaCount` / `readyReplicaCount` / `replicaShortfall`
  で返します（安定コードと安全な要約のみ。トークン・パス・リクエスト本文は含みません）。
- `mlxbarctl models resident` が上記をそのまま表示します。
- 「設定 > モデル」に、再起動待ちの注意書きと「レプリカ不足」行を該当時のみ日本語／英語で
  表示します。読み取り専用の表示で新しいミューテーションではないため、新規CLIコマンドは不要です。

## Compatibility / 互換性

- No settings-schema changes; `SettingsStore.DEFAULTS` untouched. An existing
  `config.json` needs no migration.
- New `modelPool` / `loadedModels[]` fields are additive; no existing key was
  removed, renamed or retyped. Older coordinators without them fall back safely.
- `replicas == 1` (every model's default) is byte-identical to v2.0.0.
- OpenAI / Anthropic / management API handlers, Coordinator/Worker RPC: unchanged.
- 設定スキーマの変更なし。`modelPool` / `loadedModels[]` の新フィールドはすべて増分で、
  既存キーの削除・改名・型変更はありません。`replicas == 1` は v2.0.0 とバイト同一です。

## Verification / 検証

- Python regression suite: **410 passed** (406 from v2.0.0 + 4 new). Stable in
  default and fixed order.
- `swift build --disable-sandbox`: succeeds.
- Design and invariants: `DESIGN_v2.0.1.md`. Test plan and on-hardware checks:
  `TEST_PLAN_v2.0.1.md`. Investigation: `INVESTIGATION_v2.0.0_2026-09-08.md`.

## Checksum

`fcc36c0753881191c5a73a1ed061ca1fb1426f75662bdad04b082f20e664a06a`  `MLXBar-2.0.1.dmg`
