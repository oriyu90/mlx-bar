# DESIGN v5.0.2 — Niwaki 退化生成の修正

> 対象: MLXBar v5.0.1 からの修正版（build 72）。既定は**完全に無効のまま**で、
> 通常モデル（非 bundled）の経路は v5.0.1 と変えない。
>
> 関連文書: `TEST_PLAN_v5.0.2.md`、`RELEASE_NOTES_v5.0.2.md`、`CHANGELOG.md` [5.0.2]

---

## 1. 目的

Niwaki（Qwen3.8-Flash-Next qwen4_exp）の bundled ローダー経路で発生する
退化生成（外部 API で `!!!!` のような重複・崩壊）を修正すること。

| # | 要求 | 本設計での対応 |
|---|---|---|
| R1 | Niwaki の退化生成を修正し、通常の生成（通常 forward へ戻す）にする | F-1（batch-invariant 無効化） |
| R2 | Niwaki を image 対応と誤分類しない（text のみに正す） | F-2（modalities のテンソル証拠準拠） |
| R3 | bundled / stock ローダー間で古い KV / ディスクキャッシュを共有しない | F-3（fingerprint への loaderId 混ぜ込み） |
| R4 | bundled のメモリ見積り不足と、ロード失敗時のメモリ滞留を解消する | F-4（admission strict 強制 + `_release_partial_load`） |
| R5 | 非 bundled モデルに回帰を起こさない | F-1〜F-4 の対象限定 + §5 |
| R6 | 新規 UI 文言は導入しない（Python 側の変更のみ） | 本版は Swift / UI 文言の変更なし |

---

## 2. 原因（batch-invariant bypass の実測・構造）

### 2.1 mlx-vlm 0.7.12 の batch-invariant decode が compact MoE の remap を迂回する

- mlx-vlm 0.7.12 の `LanguageModel.__call__` は、単一トークンデコード時に
  `_batch_invariant_decode` を呼びます。batch-invariant 経路は
  bundled ローダー側が用意する `CompactMasked` の `_kept_mask` /
  `_remap` ルーティングを通りません（バイパスします）。
- Niwaki の compact MoE は `_kept_mask` / `_remap` によって
  keep されたエキスパートだけを通常 forward へ流す構造です。
  batch-invariant 経路がその routing を迂回すると、
  compact MoE の前提（keep された expert だけの mix）が崩れ、
  生成が退化します。
- 構造上の差分は実行時に観測できます: bundled ロード後の
  `language_model` が batch-invariant 対応を宣言し、
  単一トークンデコードが batch-invariant 経路へ入ります。
  退化（`!!!!` など）はこの迂回と対応します。
- 非 bundled モデルは `CompactMasked` を持たないため、
  batch-invariant 経路そのものが正しく動作し、退化しません。
  よって本修正は「batch-invariant 自体が悪い」のではなく
  **「compact MoE と batch-invariant が両立しない」** ことを前提とし、
  bundled モデルにだけ通常 forward へ戻します。

### 2.2 modalities の config のみ判定が Niwaki を image 対応に誤分類する

- 従来は `config.json` の `vision_config` / `vision_tower` / `visual` /
  `image_token_id` の有無だけで `["text", "image"]` /
  `["text"]` を決めていました。
- Niwaki は `vision_config` を宣言しますが、実 export に vision テンソルは
  0 件（0/3434）ありません。config のみ判定は Niwaki を
  `["text", "image"]` と誤分類します。
- coordinator の分類（`_capabilities`）はテンソル証拠
  （`common.vision.inspect`）を要求しており、Niwaki を `["text"]` とします。
  ワーカー側と coordinator 側で modalities が不一致でした。

### 2.3 その他 bundled 経路の安全性欠陥（実測・構造）

- **キャッシュ fingerprint のローダー非考慮**: `_model_fingerprint` は
  パス・ランタイム版のみで計算されていたため、同一ディレクトリでも
  bundled ローダーと stock ローダーで同じ namespace を作り、
  古い KV を serve し得ました。
- **ロード失敗時のメモリ滞留**: vlm アダプタには mlx-lm ワーカー相当の
  cleanup が無く、失敗したロードの数十 GB（半構築テンソル）が
  coordinator がワーカーを kill するまで残っていました。
- **admission の見積り不足**: bundled モデルは余分なバッファを
  materialize しますが、`balanced` 係数（1.15x）では過小評価でした。

---

## 3. 機能設計

### F-1 batch-invariant decode 無効化（bundled 限定・fail-open）

- helper: `Workers/mlx_vlm_worker/adapter.py`
  の `_disable_batch_invariant_for_bundled()`（no-arg instance method）。
  `load()` の bundled 分岐（`load_plan["loader"].load()` 直後）から呼ばれる。
- 非 bundled（`_bundled_loader_id is None`）では何もしません（batch-invariant 経路のまま）。
- bundled 時のみ、`language_model` の instance 属性
  `_supports_batch_invariant_decode` を `False` を返す lambda で差し替えます。
  `getattr` は instance dict を優先するため、class method よりこちらが返り、
  decode が batch-invariant を経由せず通常 forward へ戻ります。
- `language_model` が無い場合、属性が無い場合、例外が発生した場合は
  ログのみで、モデルはそのままロード・生成されます（fail-open）。
  bundled ローダーの使用ログ（WARNING）で batch-invariant 無効化の事実が残ります。
- 非 bundled モデルの decode 経路・性能は v5.0.1 から変更ありません。

### F-2 modalities のテンソル証拠準拠（fail-open）

- `load()` で `common.vision.inspect(path)` の `vision` 結果により
  `self.modalities` を `["text", "image"]` / `["text"]` と決めます。
  vision テンソルの証拠を要求する点が config 判定と異なります。
- `vision.inspect` が例外を投げた場合のみ、従来の config のみ判定へ
  フォールバックします（WARNING ログ付き）。ロード自体は失敗しません。
- Niwaki は `["text"]` となり、coordinator の `_capabilities` と一致します。
  本物の vision テンソルを持つモデルは引き続き `["text", "image"]` です。

### F-3 fingerprint への loaderId 混ぜ込み

- `_model_fingerprint(path, runtime_version, loader_id=None)`。
  `loader_id` が `None` でない場合のみ
  `b"bundled:" + loader_id` を digest に混ぜ込みます
  （`None` 既定と明示 `None` は完全一致します。
  つまり非 bundled の fingerprint 値は v5.0.1 と同じです）。
- `_init_apc` と `_init_checkpoints` のどちらも
  `self._bundled_loader_id` を渡すため、bundled モデルの KV /
  ディスクキャッシュ namespace が stock ローダーのものと分離します。
- 既存のランタイム版感度は維持されます（版が変われば fingerprint は変わります）。
  ローダーが異なれば（例: `niwaki-qwen4-exp` vs `jang-osaurus-required`）異なります。

### F-4 bundled への admission strict 強制 + `_release_partial_load`

- **admission（`Coordinator/mlxbar/workers/model_pool.py`）**:
  `_cold_estimate` で `loaderId`（`loaderId` / `loader_id`）を持つ行は、
  `models.pool.memoryAdmission` が `balanced` でも `strict` 係数（1.35x）で
  見積もります。`loaderId` が無い / `None` の行は設定どおりの係数
  （`balanced` 1.15x / `strict` 1.35x）です。強制時は INFO ログが残ります。
- **`_release_partial_load`（`Workers/mlx_vlm_worker/adapter.py`）**:
  `load()` 全体を try で囲み、例外時に半構築モデルの参照を落とし
  （`model = None`、`processor = None`）、`gc.collect()` と `mx.clear_cache()` で
  残留テンソルを即時解放してから元の例外を再送出します。
  cleanup 側の例外は握りつぶし、元のロードエラーを決してマスクしません。

---

## 4. 変更ファイル一覧

| 種類 | ファイル | 内容 |
|---|---|---|
| 変更 | `Workers/mlx_vlm_worker/adapter.py` | F-1 helper + bundled 分岐からの呼び出し、F-2 `vision.inspect` による modalities + config フォールバック、F-3 fingerprint の `loader_id` 混ぜ込み + 呼び出し2箇所、F-4 `_release_partial_load` + try/except |
| 変更 | `Coordinator/mlxbar/workers/model_pool.py` | F-4 bundled 行の `strict` 強制 |
| 版数 | `Packaging/Info.plist` 他5箇所 | 5.0.2 / build 72（`Coordinator/mlxbar/__init__.py`、`Coordinator/pyproject.toml`、`scripts/build-release.sh`、`scripts/verify-release.sh` フォールバック、`README.md`） |
| 新規 | `Tests/test_v5_0_2_niwaki_regressions.py` | 収録済み（30件。`BatchInvariantDisabledTests` 等6クラス。§6 参照） |
| 新規 | `DESIGN_v5.0.2.md` / `TEST_PLAN_v5.0.2.md` / `RELEASE_NOTES_v5.0.2.md` | 本文書群 |

※ 本設計書はコードを変更する文書ではなく、実装済みの別タスクの記録である。
※ `_release_partial_load` は `load()` の失敗経路の cleanup であり、
※ 単独のテストクラスは持たず、`load()` の構造（bundled 分岐・try/except）で担保します。

---

## 5. 安全性・互換性の設計判断

### D-1 既定で同梱ローダーは無効のまま

`models.bundledLoaders` の既定は `false`。無効時は Niwaki が従来どおり
`MODEL_NEEDS_BUNDLED_LOADER` で即失敗し、v5.0.1 と同じ公開挙動です。
本版の修正は bundled 経路（`bundledLoaders == true`）の品質を高めるだけです。

### D-2 非 bundled モデルは不変（通常モデル回帰なし）

- F-1 の helper は `_bundled_loader_id is None` のとき no-op で、
  非 bundled モデルは batch-invariant 経路のままです。
- F-3 の混ぜ込みは `loader_id is None` のとき何も加えず、
  非 bundled の fingerprint 値は v5.0.1 と同一です。
- F-4 の strict 強制は `loaderId` を持つ行にだけ効き、
  通常の affine 量子化モデル（`loaderId` なし）の見積りは設定どおりです。
- F-2 の `vision.inspect` は vision テンソルを持つモデルを
  `["text", "image"]` のままにします（positive control で担保）。

### D-3 fail-open：失敗してもロードを止めない

- F-1 の差し替え失敗、`language_model` 不存在: ログのみで stock decode のままロード・生成します。
- F-2 の `vision.inspect` 例外: config のみ判定へフォールバックし、ロードを失敗させません。
- F-4 の `_release_partial_load` の cleanup 例外: 握りつぶし、元のロード例外をそのまま返します。

### D-4 新規エラーコードなし・スキーマ不変

- 新規エラーコードは設けません。`MODEL_NEEDS_BUNDLED_LOADER` /
  `MODEL_JANG_OSAURUS_REQUIRED` など v5.0.1 のコード体系はそのままです。
- `schemaVersion=1` のまま。`CATALOG_CLASSIFIER_VERSION` は `"4"` のまま
  （v5.0.2 は分類規則を変更しないため再走査不要です）。
- Python 側の変更のみで、Swift / UI 文言の変更はありません。

---

## 6. テスト方針と実機再検証手順

| 種別 | 内容 |
|---|---|
| 単体 | F-1: bundled のみ batch-invariant 無効化、非 bundled は no-op、欠落 / bare でも例外なし、`load()` が bundled 分岐で呼ぶこと |
| 単体 | F-2: Niwaki 合成（0 vision テンソル）で `vision.inspect` / 分類器とも `["text"]`、vision テンソルありは `["text", "image"]`、adapter が `vision.inspect` を参照すること |
| 単体 | F-3: loaderId で fingerprint が変わること、決定性、roader 間差分、`None` 既定の同一性、ランタイム版感度の維持 |
| 単体 | F-4: `balanced` で bundled は `strict`（1.35x）、`loaderid` snake-case も同じ、非 bundled は設定どおり、`strict` 設定では無差別、`loaderId=None` は設定どおり |
| 回帰 | `bundledLoaders` 既定 `false`、CLI / PUT / GUI 同形（`nested_patch`・`config get` / `set` の形）、`CATALOG_CLASSIFIER_VERSION == "4"`、`schemaVersion == 1` |

単体テストの実クラス名（`Tests/test_v5_0_2_niwaki_regressions.py`、30件）:
`BatchInvariantDisabledTests`（6件）、`ModalitiesVisionInspectTests`（5件）、
`FingerprintLoaderIdTests`（5件）、`AdmissionStrictForBundledTests`（5件）、
`BundledLoadersParityTests`（7件）、`CatalogVersionPinnedTests`（2件）。

期待値は `TEST_PLAN_v5.0.2.md` を参照。実測（v5.0.2）: pytest 1091 passed / 1 skipped / 0 failed
（`cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly`）、
`check_i18n` 529 keys / 0 missing（`python3 scripts/check_i18n.py`）。
Swift 側の変更が無いため swift build / swift test の v5.0.2 実測値は本版では記録しません。

### 実機再検証（本版の主目的）

Niwaki で次の順序を実機で確認します（`models.bundledLoaders == true` 前提）:

1. **Niwaki ロード**: bundled ローダーが選ばれることをワーカー log
   （`Bundled loader enabled: niwaki-qwen4-exp`）と
   `Batch-invariant decode disabled (CompactMasked bypass in mlx-vlm 0.7.12)` の
   WARNING の両方で確認します。
2. **chat / completions 正常化確認**: Chat Completions
   （`/v1/chat/completions`）と completions
   （`/v1/completions`、および Anthropic `/anthropic` 公開面）で
   実際に生成させ、`!!!!` のような退化が消えて通常の日本語・英語文が出ること
   （外部 API 退化の通常化）を目視で確認します。
3. **対照（回帰なし）**: 非 bundled の通常モデル（例: affine 量子化）でも
   同じ手順で正常生成し、batch-invariant 経路のままであること
   （ワーカー log に 無効化 WARNING が出ないこと）を対照します。

実機の期待結果は `TEST_PLAN_v5.0.2.md` §2 の A/B/C 与 API 各経路を参照。

---

## 7. 将来課題

- JANG ランタイムの内製（中期課題。v5.0.1 から継続。本版では実施しない）。
- Niwaki の長い生成の品質評価（v3.2.0 から継続。本版では変更しない）。
- mlx-vlm の batch-invariant 経路と compact MoE の両立（上流対応があれば
  F-1 の無効化は不要になる可能性。本版では bundled 限定で回避する）。
