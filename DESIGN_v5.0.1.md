# DESIGN v5.0.1 — JANG・Niwaki の取り扱い改善

> 対象: MLXBar v5.0.0 からの改善版（build 71）。既定は**完全に無効のまま**で、
> 通常モデルの経路・エラーは v5.0.0 と変えない。JANG ランタイムの内製は含まない（中期課題）。
>
> 関連文書: `TEST_PLAN_v5.0.1.md`、`RELEASE_NOTES_v5.0.1.md`、`CHANGELOG.md` [5.0.1]

---

## 1. 目的

非標準形式の2系統、JANGH2（jangtq2）と Niwaki（Qwen3.8-Flash-Next qwen4_exp）の取り扱いを、
「実機で確認した挙動」に合わせて正直にすること。

| # | 要求 | 本設計での対応 |
|---|---|---|
| R1 | JANG は MLXBar / MLX LM / MLX VLM / LM Studio / oMLX では実行不可を、カタログでもロード時でも分かるようにする | F-1（検出3条件）+ F-2（`runtimeFit` 全 `unsupported`） |
| R2 | JANG は深い読み込みで落ちる前に、根拠付きで即失敗させる | F-3（ワーカー preflight 即時失敗）+ F-4（supervisor 早期拒否） |
| R3 | JANG は Osaurus 0.25.24+ で開くよう案内する | F-3/F-4 のメッセージ + F-5（カタログバナー） |
| R4 | Niwaki は既存 bundled loader でロード可能だが、既定は無効のまま。有効化経路が分かるようにする | F-6（エラー文言・バナーの改善） |
| R5 | 既存の通常モデル（affine 量子化など）に回帰を起こさない | F-1 の狭い一致 + §5 |
| R6 | 新規 UI 文言は日本語・英語の両言語対応 | F-5（`LS()` キー + `en.lproj`） + `check_i18n` |

---

## 2. 現状調査結果（v5.0.0 実機）

### 2.1 JANGH2（Ornith-1.5-122B-JANGH2 など）

- JANGH2 のアーティファクトは、stock な mlx-lm / mlx-vlm の重み読み込みでは解釈できないレイアウトで、
  検証済みの実測では読み込み深部（`SwitchLinear.to_quantized` の rotation エラー）で失敗していた。
  理由と対策から遠く、数秒ではなく重み処理の入口を過ぎた位置で落ちる。
- MLXBar が起動できるランタイム（mlx-lm / mlx-vlm stock）は JANG に対応せず、
  LM Studio と oMLX も JANG に対応しない。よって本版の対応は「実行」ではなく
  **「事前に弾いて Osaurus 0.25.24+ へ送る」** ことである。
- カタログには、再走査しても通常行として残り、「モデル名の行でなぜロードできないか」が見えない。

### 2.2 Niwaki（Qwen3.8-Flash-Next-102B-A5B-Niwaki-v2.4-3bit-mlx など）

- 既存の同梱ローダ（`niwaki-qwen4-exp`）が実装済みで、`models.bundledLoaders == true` のときだけロード可能。
  既定は `false` で、無効時は `MODEL_NEEDS_BUNDLED_LOADER` で即失敗する（実測）。
- 既定無効のままで正しい（v3.2.0 の opt-in 設計を変えない）。本版の変更はロード可否ではなく
  **有効化経路の案内** だけである。
- 同梱ローダ自体の実装は変わっていない。v5.0.0 ではエラー文言とカタログ上の案内が弱く、
  「設定のどこを有効にすればよいか」がその場で分からなかった。

### 2.3 カタログ分類版

- `Coordinator/mlxbar/state.py` の `CATALOG_CLASSIFIER_VERSION` が `"3"` のまま。
  既存インストールは再走査されず、JANGH2 の新しい行（`jang-osaurus-required`）が付かない。
  本版は `"3" → "4"` に上げ、起動時に再走査させて既存行を更新する。

---

## 3. 機能設計

### F-1 JANG 検出の3条件

カタログ分類（`Coordinator/mlxbar/catalog/classifier.py`）とワーカー側
（`Workers/common/jang.py`）で同じ判定を共有する。いずれかの条件に一致したら JANG として扱う:

| 条件 | 判定 |
|---|---|
| (a) `jang_config.json` | `format == "jangtq2"`（大文字小文字を区別せず完全一致）、または `weight_format` が `jangtq` を部分文字列として含む |
| (b) `config.json`（再帰走査） | `quantization` 辞書の `mode` が `jangtq2` と完全一致（大文字小文字を区別せず） |
| (c) `osaurus.json` | ファイルが存在する |

読み取るのは `config.json` / `jang_config.json` と存在確認だけであり、重み（safetensors）は
読み込まず、モデル同梱の `*.py` は import・参照・実行もしない。テンソル名のスキャンもしない。

### F-2 カタログ行（`runtimeFit`・`loaderId`）

F-1 に一致した行は次の値になる:

- `loaderId: "jang-osaurus-required"`（`BUNDLED_LOADER_KEYS` の `jang_config` 項目でも候補として名付けられる）
- `reason`: JANGH2（jangtq2）量子化の検出と Osaurus 0.25.24+ の案内（日本語、既存の文体に合わせる）
- `runtimeFit`: `{"mlx-lm": "unsupported", "mlx-vlm": "unsupported", "omlx": "unsupported", "lm-studio": "unsupported"}`
- `evidence`: 証拠の短い文字列（`jangEvidence`）を残す
- 手動エンジン選択（override）でも `runtimeFit` は全 `unsupported` のまま（JANG を「ロード可能」として復活させない）

`CATALOG_CLASSIFIER_VERSION` は `"4"` に上げる（実で実装済み）。

### F-3 ワーカー preflight の即時失敗

- mlx-vlm ワーカー: `loaders.generic_preflight` の入口で共有判定 `raise_if_jang` を呼ぶ。
- mlx-lm ワーカー: `plan()` の経路を持たないため、adapter の load 入口で同じ `raise_if_jang` を呼ぶ。
- どちらも新規エラーコード `MODEL_JANG_OSAURUS_REQUIRED` を使う（設計通り同梱し、`ErrorText.swift` に日英登録、`api/anthropic_stream.py` に `invalid_request_error` の分類を追加）。汎用の `MODEL_FORMAT_UNSUPPORTED` は使わない。
- メッセージは本版の定型文（Osaurus 0.25.24+ 案内 + 「LM Studio と oMLX は JANG に対応していないため使えません」）。
  証拠は message 末尾と `detail` に残る。**絶対パスは含めない**（ファイル名・フィールド名のみ）。
- 重みの読み込み・確保の前に失敗するので、数十 GB の領域確保も、rotation エラーの深い失敗も起こらない。

### F-4 Supervisor の早期拒否

- `Coordinator/mlxbar/workers/supervisor.py::load()` の冒頭で、カタログ行の
  `loaderId == "jang-osaurus-required"` を弾き、同じ `MODEL_JANG_OSAURUS_REQUIRED` を返す。
- カタログ値だけを見るためファイルシステムへの入出力は行わず、ワーカー起動前に即失敗する。
  カタログが古く分類前の行（ワーカー側の F-3 が真実源）でも破れぬよう、二重の門にする。

### F-5 GUI カタログバナー（日英両言語対応）

`Sources/MLXBar/Models/ModelCatalogView.swift` で `loaderId` ごとにオレンジ行を表示する
（v3.2.1 のモデル警告と同スタイル。タイマーなし）:

- JANG: 「JANG jangtq2 形式です。このアプリでは実行できません。Osaurus 0.25.24+ で開いてください」
  + 「LM Studio・oMLX では実行できません。」
- Niwaki: 「このモデルは同梱ローダー（Niwaki qwen4_exp）が必要です。
  設定 → モデル → バンドル済みローダーを許可を有効にすると読み込めます」

文言は、`LS()` のキーとして `en.lproj` に英訳が登録され、日英両言語で表示されます。
実際の `.strings` は別タスクで対応済みである。

### F-6 Niwaki の有効化経路案内（改善のみ）

- `MODEL_NEEDS_BUNDLED_LOADER` の文言に「設定 → モデル → バンドル済みローダーを許可」
  の有効化経路（GUI と `mlxbarctl config set models.bundledLoaders true`）を明示する。
- Niwaki ローダ自体の実装・重み再構成・既定値（`false`）は変更しない。

---

## 4. 変更ファイル一覧

| 種類 | ファイル | 内容 |
|---|---|---|
| 変更 | `Coordinator/mlxbar/catalog/classifier.py` | F-1 検出3条件、F-2 `runtimeFit` / `loaderId`、override 時の維持 |
| 変更 | `Coordinator/mlxbar/state.py` | `CATALOG_CLASSIFIER_VERSION "3" → "4"` |
| 変更 | `Coordinator/mlxbar/workers/supervisor.py` | F-4 早期拒否 |
| 変更 | `Workers/common/jang.py` | F-1 共有判定 + F-3 `raise_if_jang` |
| 変更 | `Workers/mlx_vlm_worker/loaders/__init__.py` | F-3 `generic_preflight` への `raise_if_jang` |
| 変更 | `Workers/mlx_lm_worker/adapter.py` | F-3 共有判定 |
| 変更 | `Sources/MLXBar/Models/ModelCatalogView.swift` | F-5 バナー |
| 変更 | `Sources/MLXBar/Resources/{ja,en}.lproj/Localizable.strings` | F-5/F-6 の日英文言 |
| 変更 | `Sources/MLXBar/Services/ErrorText.swift` | 新規コード `MODEL_JANG_OSAURUS_REQUIRED` の日英登録（Osaurus 案内） |
| 変更 | `Coordinator/mlxbar/api/anthropic_stream.py` | `MODEL_JANG_OSAURUS_REQUIRED` → `invalid_request_error` の分類追加 |
| 版数 | `Packaging/Info.plist` 他6箇所 | 5.0.1 / build 71 |
| 新規 | `Tests/test_v5_0_1_jang_niwaki.py` | 収録済み（`ClassifierJangTests` / `LoaderPreflightJangTests` / `LoaderPlanNiwakiTests` / `JangArtifactPredicateTests`） |
| 新規 | `DESIGN_v5.0.1.md` / `TEST_PLAN_v5.0.1.md` / `RELEASE_NOTES_v5.0.1.md` | 本文書群 |

※ 本設計書はコードを変更する文書ではなく、実装済みの別タスクの記録である。

---

## 5. 安全性・互換性の設計判断

### D-1 既定で同梱ローダーは無効のまま

`models.bundledLoaders` の既定は `false`。無効時は Niwaki が従来どおり
`MODEL_NEEDS_BUNDLED_LOADER` で即失敗し、JANG 以外の通常経路は変わらない。

### D-2 通常 affine モデルに回帰させない

- `jangtq2` は完全一致（大文字小文字を区別せず）で比較し、`jangtq` の部分一致は
  `weight_format` のみに限定する。通常の affine 量子化モデル（例: qwen3_5_moe）は
  一致し得ない。
- 検出は config 証拠のみ（重み・テンソル名・ファイル名の単独マッチでは弾かない）。

### D-3 絶対パスを漏洩しない

- preflight・supervisor どちらの JANG メッセージにも絶対パスを含めない。
  証拠はファイル名・フィールド名（例: `jang_config.json format=jangtq2`）のみ。
- 従来の preflight が `detail` にパスを残す慣習は、JANG メッセージには適用しない
  （`common/jang.py` の `raise_if_jang` の `detail=evidence` 参照）。

### D-4 新規エラーコードを設ける・スキーマは変えない

- コードは JANG 用の新規 `MODEL_JANG_OSAURUS_REQUIRED` と、Niwaki 用の既存 `MODEL_NEEDS_BUNDLED_LOADER`。
  `ErrorText` への日英登録と `anthropic_stream` の分類追加を設計通り行う（汎用 `MODEL_FORMAT_UNSUPPORTED` で案内が埋もれるのを防ぐ）。
- `schemaVersion=1` のまま。追加はカタログ行の additive フィールドのみで、
  旧設定ファイル・旧カタログはそのまま読める（再走査で値が更新される）。

### D-5 JANG ランタイム内製は中期課題（次回以降）

- 本版では JANG ランタイムを MLXBar に内製しない（mlx-lm / mlx-vlm stock では実行できないため）。
  ユーザーは Osaurus 0.25.24+（または対応する MLX Studio / jang-tools）で JANG を実行する。
- 内製（JANG デコーダの実装）は重み形式の互換・性能検証を要する中期課題として、
  次回以降の版で計画する。本版の「即時失敗と案内」は、その計画が進むまでユーザーに
  真実を伝えるための措置である。

---

## 6. テスト方針

| 種別 | 内容 |
|---|---|
| 単体 | F-1 の3条件（(a) `format`/`weight_format`、(b) 再帰 `mode`、(c) `osaurus.json`）と非一致（通常 affine） |
| 単体 | F-2 の `runtimeFit` 全 `unsupported`・`loaderId`・override 時の維持 |
| 単体 | F-3/F-4 の `MODEL_JANG_OSAURUS_REQUIRED`（w/ Osaurus 案内、絶対パスなし） |
| 単体 | F-6 の `MODEL_NEEDS_BUNDLED_LOADER` 文言に有効化経路を含むこと |
| 回帰 | 通常モデル（affine など）の分類・preflight が従来と同じであること |
| GUI | `check_i18n` 欠落 0、`swift test` 全パス |

単体テストの実クラス名（`Tests/test_v5_0_1_jang_niwaki.py`）: `ClassifierJangTests`（分類・`runtimeFit`）、`JangArtifactPredicateTests`（F-1 の3条件・affine 非一致）、`LoaderPreflightJangTests`（preflight・`plan()` の JANG 即時失敗）、`LoaderPlanNiwakiTests`（Niwaki の `bundledLoaders` ON/OFF）。Osaurus 案内は新規コード `MODEL_JANG_OSAURUS_REQUIRED` で検証する。

期待値は `TEST_PLAN_v5.0.1.md` を参照。実測（v5.0.1）: pytest 1061 passed / 1 skipped / 0 failed（`cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly`、flaky 修正後）、`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift build` パス（警告のみ）、`swift test` 45 tests / 0 failures（同 `DEVELOPER_DIR`、`--disable-sandbox`）、`check_i18n` 529 keys / 0 missing。実機 `plan_load`: A（JANG）は `bundled True/False` どちらでも `MODEL_JANG_OSAURUS_REQUIRED`、B（Niwaki）は `False` で `MODEL_NEEDS_BUNDLED_LOADER` / `True` で plan 成功、C（回帰）は pass。既知 flaky の `test_buffered_tool_generation_still_emits_heartbeats`（`Tests/test_worker_server.py`）は interval を 0.03→0.10 に緩めて安定化した（ロジック無変更）。

---

## 7. 将来課題

- JANG ランタイムの内製（中期課題。次回以降。本版では実施しない）。
- Niwaki の長い生成の品質評価（v3.2.0 からの継続課題。本版では変更しない）。
