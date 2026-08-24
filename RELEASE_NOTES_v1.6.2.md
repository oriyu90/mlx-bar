# MLXBar v1.6.2

## Multiple models, with memory treated as a safety contract / 複数モデルをメモリ契約付きで常駐

OpenAI-compatible requests can now select different local MLX models without unloading the previous one first. Each resident model runs in an isolated worker process. Cold loads and generation remain globally serialized, avoiding overlapping allocation peaks and preserving v1.6.1 request behavior.

OpenAI互換APIで別のMLXモデルを指定しても、先のモデルをすぐに解放せず、独立したWorkerプロセスで常駐できます。コールドロードと生成は安全のため引き続き全体で1件ずつです。

Admission requires all of the following: the conservative model charge fits its per-model cap; aggregate reservations fit the pool budget; the resident-count cap can be met; macOS is not under memory pressure; and current reclaimable memory still leaves the configured system reserve. The accepted reservation is installed inside that worker through MLX 0.32.1 `set_memory_limit` before weights load, then verified through the runtime contract. A runtime unable to prove that contract is rejected safely.

モデル単位上限、全体予算、常駐数、macOSメモリ圧、システム予備のすべてを満たしたときだけロードします。承認した予約値は重みを読む前にMLX allocatorへ設定し、Workerの応答で同じbyte値を確認します。新しいランタイムがこの契約を証明できない場合、poolでは失敗側へ倒します。

API-loaded models unload after the configured idle TTL. Manually loaded and `keepLoaded` profile models are retained, but an idle pinned model may still be released under critical macOS pressure. Active streams hold a lease that blocks TTL, LRU, and live budget reductions until every completion, cancellation, disconnect, or exception path has released it.

APIが自動ロードしたモデルは設定TTLの無使用後に解放されます。手動ロードと`keepLoaded`プロファイルは保持しますが、macOSがcritical pressureを報告した場合はマシンを守ることを優先します。生成中はleaseが付き、正常終了・中断・切断・例外の全経路で外れるまで解放されません。

Runtime updates now snapshot every resident model for the target engine, drain only that engine, probe the candidate's MLX memory-limit contract, and reload all residents. A failed activation returns to the previous runtime and restores the same set. No model-name or architecture allowlist is used, so new model releases follow catalog and runtime capability contracts instead of requiring MLXBar patches.

## LM Studio

LM Studio v1 REST instances remain on the existing provider path. MLXBar now unloads by the returned `instance_id`, but does not count an externally managed LM Studio process inside a native MLX byte budget. Switching to LM Studio first drains native residents, so the UI never claims memory coverage it cannot enforce. LM Studio's own Resource Guardrails, TTL, and Auto-Evict remain authoritative.

## Visibility / 表示

The Model settings page shows resident count, reserved memory, aggregate budget, maximum residents, per-model default cap, idle TTL, system reserve, and total ratio. `GET /api/v1/status` adds `loadedModels` and `modelPool` while preserving the legacy `loadedModel` primary field.

## Verification / 検証

- Python regression suite: 289 passed.
- Swift Debug build: passed.
- Swift Release build and signed/ad-hoc DMG verification: passed; details are recorded in `TEST_PLAN_v1.6.2.md`.
- Detailed invariants and 2026-08 primary-source basis: `DESIGN_v1.6.2.md`.

## Checksum

`128ab0c685ce6f912cc76b22c7654834bee45a85cac68c46aa34b36933993f1f`  `MLXBar-1.6.2.dmg`
