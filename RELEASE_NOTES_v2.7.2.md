# Release Notes v2.7.2

## モデル要求の厳格解決と常駐認識の安定化

### 直したこと

- **モデル要求を無視して常駐モデルが応答する不具合を修正**しました。
  常駐が1つだけのとき、カタログに実在する別モデルを `model` に指定しても
  常駐モデルが黙って応答していました（`maxResidentModels=1` を含む）。
  本版からは要求どおりに切り替えます。待機中なら自動で載せ替え、
  生成中なら `ENGINE_BUSY` (429)、固定モデルで入りきらなければ
  `MEMORY_BUDGET_EXCEEDED` (503) で明示拒否します。
- **常駐モデルが別モデルにすり替わって認識される不具合を修正**しました。
  実在モデルが `x` 等の汎用別名と同名の場合に最終使用モデルへ誤解決する順序、
  汎用別名の要求で主表示（primary）が先着常駐へ跳ぶ動作、
  `provider_key` 未設定の `none` 誤一致を修正しました。
  汎用別名（`loaded`/`local`/`x` 等）は従来どおり常駐へ解決し、主表示も安定します。
- **`max_tokens` 省略時の既定値とモデル一覧の上限表示を要求モデル基準へ修正**しました。
  複数常駐で主モデルと要求が違うとき、別モデルの上限で切り詰めません。

### 互換性

- 設定の追加のみで、既定値の変更はありません。既存の `config.json` はそのまま使えます。
- 汎用別名の解決は従来どおりです。**カタログにない任意のモデル名は、
  単一常駐でも常駐へ振り分けず `MODEL_NOT_FOUND` (404) で明示拒否します**。
  クライアント側の `Model` には `GET /v1/models` の表示名または内部IDを指定してください。
- `replicas = 1`・`generationConcurrency = 1` の構成の挙動は従来どおりです。

---

## Release Notes v2.7.2 (English)

### Fixed

- **Requests answered by the resident model instead of the requested one.**
  With exactly one model resident, asking for a different catalog-known model
  silently kept answering with the resident (including `maxResidentModels=1`).
  Requests are now honored: idle pools switch automatically, busy pools refuse
  loudly with `ENGINE_BUSY` (429), and a pinned resident that cannot fit refuses
  with `MEMORY_BUDGET_EXCEEDED` (503).
- **A resident model recognized as a different model.** Fixed the resolution
  order that sent a real model named like a generic alias (`x`, etc.) to the
  last-used model, generic-alias requests flipping the primary headline to the
  first resident, and `provider_key: None` matching a `none` request.
  Generic aliases (`loaded`/`local`/`x`, etc.) still resolve to the resident
  and the primary stays stable.
- **Server-default `max_tokens` and the model-list ceiling now follow the
  requested model**, not the pool primary, so concurrent residents no longer
  truncate each other.

### Compatibility

- Additive changes only; no default values changed. Existing `config.json` files keep working.
- Generic-alias resolution is unchanged. **Unknown model names now fail loudly
  with `MODEL_NOT_FOUND` (404) even with a single resident** instead of being
  absorbed. Set the client `Model` to a display name or internal ID from
  `GET /v1/models`.
- Behaviour with `replicas = 1` or `generationConcurrency = 1` is unchanged.
