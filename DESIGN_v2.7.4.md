# DESIGN v2.7.4 — 別名形実在モデルの優先解決 / キュー＋逼迫時の非常駐即時解放

- 版数: app 2.7.4 / build 51。Coordinator `__version__` 2.7.4。`schemaVersion` は 1 のまま（追加のみ）。
- 方針: 既定値不変、追加のみ、検証失敗は fail-closed。Coordinator が正本、GUI/CLI は薄いクライアント。
- v2.7.3 の厳格上限（空きがある間は追加、満杯は `RESIDENT_LIMIT_REACHED`）は維持する。Swift の変更なし。

## 1. 背景

- v2.7.3 精査で別名 shadowing の残り1経路を検出。実在モデル ID が汎用別名形
  （例 `x`）の場合、常駐 A 中に `x` 要求が `loaded` フォールバック経由で
  A に誤解決された（`find` 別名経路に加え `loaded` 単一フォールバックも
  別名一致していたため）。
- オーナー要求：上限未満は追加、上限到達はエラー、常駐決定モデルは応答後も
  規定数でメモリ保持、非常駐は最終要求からの設定時間後に解放、
  ただしキュー＋逼迫時は経過を待たず解放。

## 2. 修正内容

### 2.1 別名形実在モデルの優先（`openai_compat.py`）

- `_find_exact_model`（カタログ exact のみ、別名 fallback なし）と
  `_is_exact_match`、`_honor_resident_alias` を新設。
  exact 一致は常に honor、汎用別名はカタログ exact が別 ID を指す場合のみ
  alias を退け自動ロードへ、未知の非汎用名は honor しない（404 維持）。
- `find` 返却の2箇所と `loaded` 単一フォールバックの2箇所の計4箇所を
  `_honor_resident_alias` 経由へ統一。Anthropic 互換・legacy 補完も同一
  `_ensure_requested_model` のため同時に正される。
- `_model_matches` は呼出がなくなったため残置（外部互換のための定義保持）。

### 2.2 キュー＋逼迫時の非常駐即時解放（`model_pool.py`）

- `_reap_once` でプール queue backlog を検出。リース保持スロット
  （生成中・queue 待機は入場前に lease 加算済み）は従来どおり保護したまま、
  それ以外の待機中非固定スロットは `pressure >= 4` 単独に加え
  `queue backlog あり かつ pressure >= 2` でも TTL を待たず解放する。
  queue なしの warning 逼迫（2–3）では従来どおり TTL まで保持する。
- 固定・リース中・replica 0・最終レプリカの保護、設定引下げ時の縮退、
  メモリ予算退避は無変更。

## 3. 互換性・安全性の不変条件

- 設定スキーマ追加なし（`DEFAULTS` 不変、`schemaVersion` 1）。既定値不変。
- 待機中要求の lease 保護により、解放が queue 待機を壊さない。
  拒否・解放はプール状態を壊さず、要求の成否のみに影響する。
- UI：Swift 無変更。新規 `.strings` なし。新エラーコードなし
 （`RESIDENT_LIMIT_REACHED` は v2.7.3 から継続）。

## 4. 検証（`TEST_PLAN_v2.7.4.md` に記録）

- 新規・更新：別名形実在モデルの自動ロード（`test_alias_shaped_real_model...`）、
  queue＋逼迫の即時解放、queue なし warning 逼迫の保持。
- 既存回帰は新規込みの全緑（既知 fluke
  `test_ttl_starts_when_a_slow_load_finishes` を除く）。
- `swift build` 成功（Swift 無変更のため再確認のみ）。
