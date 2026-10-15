# Release Notes v2.7.3

## 常駐数上限の厳格化

### 変わったこと

- **常駐モデル数の上限 (`maxResidentModels`) を厳格化**しました。
  上限に達していないうちは、要求されたモデルを新しい常駐として追加して
  ロードします。上限に達している状態で新しい異なるモデルを要求した場合は、
  古い常駐を黙って追い出さず `RESIDENT_LIMIT_REACHED` (409) で明示エラーにします。
  切り替えるときは不要な常駐をアンロードしてから再要求してください。
  同一モデルの再ロードやレプリカは上限に数えず、満杯でも利用できます。

### 互換性

- 設定の追加のみで、既定値の変更はありません。既存の `config.json` はそのまま使えます。
- v2.7.2 の厳格解決（未知モデル名の 404・汎用別名の常駐解決）はそのままです。
- `replicas = 1`・`generationConcurrency = 1` の構成の挙動は従来どおりです。

---

## Release Notes v2.7.3 (English)

### Changed

- **Strict resident-count limit (`maxResidentModels`).** While the pool has room,
  a requested model is added (loaded) as a new resident. Once distinct residents
  reach the limit, asking for another distinct model fails loudly with
  `RESIDENT_LIMIT_REACHED` (409) instead of silently evicting an idle resident.
  Unload an unneeded resident, then retry. Same-model reloads and replicas never
  count as new and stay allowed.

### Compatibility

- Additive changes only; no default values changed. Existing `config.json` files keep working.
- The v2.7.2 strict resolution (404 for unknown names, resident resolution for
  generic aliases) is unchanged.
- Behaviour with `replicas = 1` or `generationConcurrency = 1` is unchanged.
