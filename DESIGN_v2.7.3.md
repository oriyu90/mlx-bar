# DESIGN v2.7.3 — 常駐数上限の厳格化（空きがある間は追加、満杯は明示エラー）

- 版数: app 2.7.3 / build 50。Coordinator `__version__` 2.7.3。`schemaVersion` は 1 のまま（追加のみ）。
- 方針: 既定値不変、追加のみ、検証失敗は fail-closed。Coordinator が正本、GUI/CLI は薄いクライアント。
- v2.7.2 の厳格解決・常駐認識の安定化は維持し、入場時の追い出しだけを厳格化する。Swift の変更なし。

## 1. 背景（オーナー要求）

- 上限に達していないうちは常駐モデルに追加して要求モデルをロードする。
- 常駐モデルで上限に達している場合はエラーを返す。

従来（v2.7.2 以前）の `_admit` は、異なるモデル数の上限 (`maxResidentModels`)
に対しても空きがなければ最古の未固定・待機中モデルを黙って追い出して
新モデルを収容した。常駐集合が API 要求だけで静かに入れ替わり、
「いつの間にか別のモデル」・「固定ではないのに消えた」という認識ずれの温床だった。
本版からは数の上限を追い出しで満たさず、明示エラーにする。

## 2. 修正内容

### 2.1 `_admit` の数上限を追い出し対象から除外（`model_pool.py`）

- 入場開始時に異なるモデル ID 集合を数える。要求 ID が既存集合にない
  （＝新規の異なるモデル）のに `len(existing) >= maxResidentModels` なら、
  即座に `RESIDENT_LIMIT_REACHED` (409, retryable False) で拒否する。
  同一モデルの再ロード／レプリカは新規と数えず、従来どおり許可する。
- メモリ予算の退避は維持する。予算超過時のみ未固定・待機中から
  最古順に退避し、収まらなければ従来の `MEMORY_BUDGET_EXCEEDED` (503)。
- 末尾の安全網として、同時の設定引下げ競合で上限超過が残った場合も
  `RESIDENT_LIMIT_REACHED` で拒否する（`MEMORY_BUDGET_EXCEEDED` と区別）。
- 新コード `RESIDENT_LIMIT_REACHED` の文言は
  「常駐モデル数が上限（N）に達しているため、新しいモデルをロードできません。
  不要な常駐モデルをアンロードしてから再実行してください」。
  状態を変えず、要求の成否のみに影響する。

### 2.2 影響範囲の整理

- API 自動ロード（`load_for_api`, `pin=False`）と明示ロード
  （GUI/CLI, `pin=True`）の双方が同一 `_admit` を経由するため、
  上限満杯の新規異なるモデルは双方で 409 になる。切替には
  `model unload <id>`（または `DELETE /models/loaded`）で明示解放してから
  再要求する。同一モデルの並列レプリカ拡張（`_maybe_expand_replica`）と
  固定レプリカの補充（`_scale_up_pinned_replicas`）は異なるモデルを増やさない
  ため影響を受けない。
- ランタイム切替の `reload_resident` は固定優先・非固定ベストエフォートのまま。
  非固定の再収容不可は警告 skip（従来則）で更新失敗にしない。
- 設定引下げ時の live 縮退（`_reap_once` の上限 enforcement）は維持する。
  保存済み上限を下げた場合のみ最古の未固定から縮退する。

## 3. 互換性・安全性の不変条件

- 設定スキーマ追加なし（`DEFAULTS` 不変、`schemaVersion` 1）。既定値不変。
- メモリ安全性：予算・圧力・per-model 上限・lane ガード・退避抑止・critical
  pressure の固定解除は無変更。数の厳格化はゲートを強める方向であり、弱めない。
- クラッシュ安全性：追加の `await` なし（上限検査は同期のみ）。
  拒否はプール状態を変えない。
- UI：Swift 無変更。新規 `.strings` なし。`keepLoaded` 表示則は不変。
  新エラーコードは管理 API・OpenAI/Anthropic の既存エラー封筒で返り、
  日英のコード解決がなくても `code`＋`message` で表示される。
- v2.7.2 の厳格解決（未知 404・汎用別名維持・exact-first・`none` 除外）は維持。

## 4. 検証（`TEST_PLAN_v2.7.3.md` に記録）

- 新規 `Tests/test_v2_7_3_resident_limit.py` 4件：上限未満の追加・満杯の
  409 と非退避・同一モデル再ロード許可・明示 pin も厳格。
- 既存 3件は意図的変更に合わせ更新：`test_third_model...` と
  `test_manual_pin...` は 409 期待・集合不変へ、`test_max_resident...` は
  `RESIDENT_LIMIT_REACHED` 期待へ。`test_v2_7_2...::test_max1_switches...` は
  409 後 `unload`→再要求で載る手順へ更新。
- 既存回帰は新規込みの全緑（既知 fluke
  `test_ttl_starts_when_a_slow_load_finishes` を除く）。
- `swift build` 成功（Swift 無変更のため再確認のみ）。
