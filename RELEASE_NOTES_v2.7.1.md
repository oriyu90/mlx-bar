# Release Notes v2.7.1

## 常駐の固定化と最新ランタイム導入の修正

### 直したこと

- **非固定モデルが固定モデルを差し置いて固定されることがある不具合を修正**しました。
  再起動後の復元は固定プロファイルだけを固定復帰し、最後に使った非固定モデルは
  一時扱い（未使用時間後に解放）のまま戻ります。ランタイム切替後の復元は固定を
  優先し、入りきらない非固定は更新失敗にせず見送ります。固定解除（ピン外し）は
  その場で一時扱いへ戻り、行の「固定」表示も消えます。通常の手動ロードは従来
  どおり停止まで保持されます。
- **`mlxbarctl model load` に `--no-pin` を追加**しました。一時ロード（未使用時間後に
  解放）に使えます。付けない場合は従来どおり固定です。
- **最新の mlx-vlm / mlx-lm での導入検査を強化**しました。生成時に初めて使う部品の
  有無を導入前に確認し、壊れたランタイムを有効化しません。mlx-vlm 0.7.8・
  mlx-lm 0.32.0 での実生成（通常・KV量子化・2ターン目の再利用）を確認済みです。
- **mlx-vlm の tool 呼び出しの検出を更新**しました。新しい部品配置に先に対応し、
  古いランタイムでは従来の方法へ自動で戻ります。

### 互換性

- 設定の追加のみで、既定値の変更はありません。既存の `config.json` はそのまま使えます。
- ロード API の `pin` を省略した場合は従来どおり固定です。
- `replicas = 1`・`generationConcurrency = 1` の構成の挙動は従来どおりです。

---

## Release Notes v2.7.1 (English)

### Fixed

- **An unpinned model could displace a pinned model and become pinned.** After a
  restart, only pinned profiles come back pinned; the last-used unpinned model
  returns as temporary (released after its idle time). After a runtime switch,
  pinned models are restored first and unpinned models that no longer fit are
  skipped without failing the update. Unpinning now returns the model to
  temporary status immediately and clears the "Pinned" row badge. Ordinary
  manual loads are still kept until stopped, as before.
- **Added `--no-pin` to `mlxbarctl model load`** for temporary loads (released
  after idle). Without it, loads stay pinned as before.
- **Hardened the latest mlx-vlm / mlx-lm introduction checks.** Parts first used
  at request time are now verified before a runtime is staged, so a broken
  runtime is never activated. Real generations on mlx-vlm 0.7.8 and mlx-lm 0.32.0
  (plain, KV-quantized, second-turn reuse) are verified.
- **Updated mlx-vlm tool-call detection** for the new module layout, falling back
  to the previous method on older runtimes.

### Compatibility

- Additive changes only; no default values changed. Existing `config.json` files keep working.
- Omitting `pin` in the load API keeps the previous pinned behaviour.
- Behaviour with `replicas = 1` or `generationConcurrency = 1` is unchanged.
