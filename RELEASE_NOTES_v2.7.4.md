# Release Notes v2.7.4

## 別名形モデルの解決修正と逼迫時の非常駐解放

### 直したこと

- **ID が汎用別名形（例 `x`）の実在モデルを要求したとき、常駐モデルに
  誤解決されることがある不具合を修正**しました。実在する別モデルは
  要求どおりにロードします。純粋な別名は従来どおり常駐へ解決し、
  未知のモデル名は `MODEL_NOT_FOUND` (404) のままです。
- **キューが詰まりメモリ逼迫があるとき、待機中の非常駐モデルを
  解放待ち時間なしで解放**します。生成中・待機中の要求自体は保護され、
  逼迫もキューもないときは従来どおり最終要求からの時間で解放します。

### 互換性

- 設定の追加のみで、既定値の変更はありません。既存の `config.json` はそのまま使えます。
- v2.7.3 の厳格上限（上限未満は追加、上限到達は `RESIDENT_LIMIT_REACHED`）はそのままです。
- `replicas = 1`・`generationConcurrency = 1` の構成の挙動は従来どおりです。

---

## Release Notes v2.7.4 (English)

### Fixed

- **A real model whose ID looks like a generic alias (e.g. `x`) could resolve
  to the current resident.** It now loads as requested. Pure aliases still
  resolve to the resident, and unknown names stay `MODEL_NOT_FOUND` (404).
- **Idle non-resident models are shed without waiting for the idle timeout
  when requests are queued under memory pressure.** Live and queued requests
  stay protected; without pressure or a queue, the timeout still applies.

### Compatibility

- Additive changes only; no default values changed. Existing `config.json` files keep working.
- The v2.7.3 strict limit (add while room, `RESIDENT_LIMIT_REACHED` when full) is unchanged.
- Behaviour with `replicas = 1` or `generationConcurrency = 1` is unchanged.
