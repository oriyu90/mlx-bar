# MLXBar v1.6.2 テスト計画と結果

対象: メモリ安全な複数モデル常駐、API JIT load、TTL/LRU解放、複数常駐のままのruntime update/rollback。

## 自動検証

```sh
Coordinator/.venv/bin/python -m pytest Tests -q
swift build --disable-sandbox -c debug
swift build --disable-sandbox -c release
```

2026-08-24結果:

- Python: **289 passed**
- Swift Debug: **成功**
- Swift Release: **成功**

v1.6.2の新規契約:

| 契約 | 検証 |
|---|---|
| 2モデルを独立Workerで常駐 | `test_two_models_stay_in_independent_workers` |
| 同一モデルの同時loadは1回 | `test_concurrent_requests_singleflight_one_load` |
| 3個目は最古のidle API modelをLRU解放 | `test_third_model_evicts_oldest_idle_api_model` |
| 手動pinは通常LRUから保護 | `test_manual_pin_is_never_lru_evicted` |
| TTLは非固定だけ解放 | `test_ttl_reaps_only_unpinned_idle_models` |
| critical pressureはidle pinも解放 | `test_critical_pressure_overrides_idle_pin` |
| 個別上限超過をWorker起動前に拒否 | `test_per_model_limit_rejects_before_worker_creation` |
| MLX allocator上限はマシン上限ではなく予約値 | `test_allocator_limit_is_the_admitted_reservation_not_the_machine_cap` |
| ロード後実測は事前予約を超えられない | `test_post_load_measurement_cannot_exceed_the_admitted_reservation` |
| stream closeでleaseを必ず解放 | `test_generation_lease_is_released_when_stream_closes` |
| pool有効/無効は次回起動までlatch | `test_enabled_toggle_is_latched_until_service_restart` |
| live常駐数縮小はidle LRUから適用 | `test_live_resident_reduction_evicts_oldest_unpinned_model` |
| 異常終了したidle Workerはpinにかかわらず回収 | `test_dead_idle_worker_is_removed_even_when_pinned` |
| ロード中のunloadは完了を待ちslotを残さない | `test_unload_waits_for_an_inflight_cold_load` |
| TTLは遅いロードの開始ではなく完了から計時 | `test_ttl_starts_when_a_slow_load_finishes` |

## 配布物検証

- `scripts/build-release.sh`でarm64 appとDMGを生成。
- `scripts/verify-release.sh`でversion/build番号、署名、必要ファイル、不要bytecodeの非収録を確認。
- DMGを`hdiutil verify`で検証しSHA-256をrelease notesと`.sha256`へ記録。

2026-08-24結果: **全項目成功**。DMG SHA-256は外部のrelease notesと
`.sha256` sidecarへ記録する（DMG自身へchecksumを埋め込む循環は作らない）。

## 実機メモリ検証

次はリリース可否と分離した現場検証とする。モデル本体と必要な空きメモリがこのビルド環境にない場合、実施したと装わない。

1. 2モデルをAPIで順番に指定し、`loadedModels=2`と各Worker PIDが異なること。
2. 各Workerの`memoryLimits.set_memory_limit`が表示予約と一致すること。
3. 合計予算より小さい上限で新要求が503になり、既存生成が継続すること。
4. TTL後にAPI自動ロードモデルだけが解放されること。
5. runtime update成功で全常駐モデルが戻り、probe失敗で旧slotへrollbackすること。
6. CoordinatorをSIGKILL後に起動し、孤児Worker/socketが回収されること。
