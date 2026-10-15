# DESIGN v2.7.2 — モデル要求の厳格解決 / 常駐認識の安定化

- 版数: app 2.7.2 / build 49。Coordinator `__version__` 2.7.2。`schemaVersion` は 1 のまま（追加のみ）。
- 方針: 既定値不変、追加のみ、検証失敗は fail-closed。Coordinator が正本、GUI/CLI は薄いクライアント。
- v2.7.1 の挙動・表示・設定は、意図的な2件の修正を除き不変。Swift の変更なし。

## 1. 背景（オーナー報告の2件）

1. モデル要求を無視して常駐モデルを返してくる。
2. 常駐モデルがいつの間にか別のモデルにすり替わって認識される。

調査の結果、(1) は `_ensure_requested_model` の単一常駐エイリアス shortcut が
`maxResidentModels == 1` を含むあらゆる非一致名を常駐へ吸収していたことだった。
カタログに実在する別モデルへの要求（例: A 常駐中に B を要求）も自動ロード経路へ
到達せず、常駐 A が黙って応答した。エラーは出ないため、呼び出し側は誤ったモデルで
生成したことに気づけない。

(2) は3つの重なりだった。

- `find_loaded_model` が汎用エイリアス（`loaded`/`local`/`x` 等）で先着スロットを
  返し、primary をその場で書き換えた。汎用名の要求だけで primary が跳び、
  `status().loadedModel`（ヘッダー）と GUI の主表示がすり替わった。
- `_find_model` がエイリアス判定を exact 照合より先に行った。実在モデルが
  `x` 等の汎用名と同名の場合、要求 `x` が最終使用モデル（`last_loaded_model_id`）
  の別モデルへ解決された。
- `provider_key: None` を `str(None) == "none"` として候補化していた。
  要求 `none` が provider_key のない全ローカルモデルに一致した。

いずれもクラッシュはしないが、誤ったモデルで応答・表示する静かな誤動作であり、
互換性・安全性の観点で fail-loud（404/503/429）へ正す。

## 2. 修正内容

### 2.1 厳格なモデル解決（`openai_compat.py`）

- 単一常駐 shortcut（`loaded_models()` が1件なら無条件にそれを返す分岐）を削除。
  汎用エイリアスは `find_loaded_model` の正規経路で解決し、それ以外の名は
  カタログ解決（`_find_model`）へ落とす。実在する別モデルは自動ロード経路
  （`ENGINE_BUSY` 判定＋`_admit` 退避を含む）へ進む。未知名は `MODEL_NOT_FOUND`
  (404) で明示拒否し、誤ったモデルでの黙示応答をやめる。
- ロック内再検査を `find_loaded_model`＋primary の両方へ拡張。待ち行列で
  待った要求が、待機中に常駐化した非 primary モデルを再ロードせず再利用する。
- `max_tokens` 省略時の既定値と `/v1/models` の `max_tokens` 表示を、
  プール primary ではなく要求モデルの上限から求める
 （`effective_max_tokens_for` 新設。欠落時は従来の primary へ fallback）。
  複数常駐で primary と要求が違うとき、別モデルの上限で切り詰めない。

### 2.2 常駐認識の安定化（`model_pool.py` / `openai_compat.py`）

- 候補値を空でない `str` のみに限定（`None` 除外）。`none` 要求の誤一致を消す。
- `find_loaded_model` を二段階化。第一段は exact（id/name/provider_key、
  `openai/` 接頭辞剥離、casefold）のみで一致し、一致時のみ primary を更新する。
  第二段は汎用エイリアスのみで、primary が常駐中ならそれを返し、なければ
  先着 ready を返す。エイリアス要求では primary を書き換えない。
- `_find_model` も exact-first へ。実在の `x` は `last_loaded` より優先される。
  汎用エイリアスの `last_loaded` 解決・単一カタログ時の単一返却は維持。
- `_matches`（legacy 単一経路用）は exact-first＋alias の合成を維持し、
  候補の `None` 除外のみ適用。プール経路の二段階解決を侵さない。

## 3. 互換性・安全性の不変条件

- 設定スキーマ追加なし（`DEFAULTS` 不変、`schemaVersion` 1）。`pin` 省略時の
  既定・TTL・レプリカ・admission（`_admit`）・lane ガード
  （`_admit_concurrent`）・退避抑止（`leases > 0`）・critical pressure の
  固定解除は無変更。固定（`keepLoaded`/`session_pinned`）モデルは API 自動
  ロードで追い出さない。`maxResidentModels == 1` で固定モデルが常駐中の別モデル
  要求は `MEMORY_BUDGET_EXCEEDED` (503, retryable) で明示拒否する。
- クラッシュ安全性：追加の `await` なし（解決は同期のみ）。既存の shield 付き
  `aclose`・`finally` 解放順序は不変。未知名の 404 は要求の成否のみに影響し、
  プール状態を変えない。
- UI：Swift 無変更。新規 `.strings` なし。`keepLoaded` 表示則
  （`keep_loaded or session_pinned`→「固定」）は不変。README の1行
  （単一常駐時の任意名振り分け）は厳格解決の記述へ更新する。
- 汎用エイリアス（`loaded`/`current_model`/`local`/`x`/`openai/x`、
  `openai/<name>` 形）は従来どおり常駐へ解決する。未知の任意名（typo 等）のみ
  404 へ変わる破壊的変更であり、CHANGELOG・README・リリースノートで明示する。

## 4. 検証（`TEST_PLAN_v2.7.2.md` に記録）

- 新規 `Tests/test_v2_7_2_model_routing.py` 7件：max1 での既知別モデル切替、
  未知名 404、汎用エイリアス維持、exact-over-alias（`_find_model` と `find`）、
  エイリアスでの primary 安定、`none` 非一致、モデル別既定上限。
- 既存 `Tests/test_openai_tools.py::test_a_made_up_model_name_routes_to_the_sole_resident_instead_of_erroring`
  は厳格解決に合わせて 404 期待へ更新（意図的変更）。
- 既存回帰は新規込みの全緑（既知 fluke
  `test_ttl_starts_when_a_slow_load_finishes` を除く）。
- `swift build` 成功（Swift 無変更のため再確認のみ）。
