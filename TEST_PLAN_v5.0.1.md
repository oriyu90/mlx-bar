# TEST PLAN v5.0.1 — JANG・Niwaki の取り扱い改善

対象: MLXBar v5.0.1（build 71）。v5.0.0 からの改善版。
設計: [`DESIGN_v5.0.1.md`](DESIGN_v5.0.1.md)

---

## 1. 自動テスト（必須・全パス）

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --disable-sandbox
python3 scripts/check_i18n.py
```

| 項目 | v5.0.1 実測 |
|---|---|
| pytest | 1061 passed / 1 skipped / 0 failed（`cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly`、flaky 修正後） |
| swift build | pass（`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift build`、警告のみ） |
| swift test | 45 tests / 0 failures（`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --disable-sandbox`） |
| check_i18n | 529 keys / 0 missing（`python3 scripts/check_i18n.py`） |

既知 flaky の `test_buffered_tool_generation_still_emits_heartbeats`（`Tests/test_worker_server.py`）は `heartbeat_interval_seconds` を 0.03→0.10 に緩めて安定化した（ロジック無変更。v5.0.1 実測では上記 pytest 全パスで確認）。

### 1-1. 新規テスト（`Tests/test_v5_0_1_jang_niwaki.py`）

本テストファイルは収録済みである（10件の単体テストを含む pytest 全体で 1061 passed / 1 skipped / 0 failed）。

期待される確認内容（実装済みの仕様に対応。実クラス名で記載）:

| クラス（実名） | 確認内容 |
|---|---|
| `JangArtifactPredicateTests` | 検出3条件（(a) `jang_config.json` の `format`/`weight_format`、(b) `config.json` 再帰 `quantization.mode`、(c) `osaurus.json` 存在）、通常 affine 非一致 |
| `ClassifierJangTests` | `loaderId: jang-osaurus-required`、`runtimeFit` 全 `unsupported`、Niwaki の `loaderId` 維持、通常 affine は `loaderId` なし |
| `LoaderPreflightJangTests` | `generic_preflight` / `plan()` が `MODEL_JANG_OSAURUS_REQUIRED` + Osaurus 0.25.24+ 案内を返し、絶対パスを含まない（`bundledLoaders` True/False どちらでも同じ） |
| `LoaderPlanNiwakiTests` | `bundledLoaders=false` 時の `MODEL_NEEDS_BUNDLED_LOADER`、`true` 時の同梱ローダー `niwaki-qwen4-exp` へのルーティング |

### 1-2. 回帰（既存テスト）

- 通常モデル（affine 量子化など）が JANG 検出に一致せず、従来の分類・preflight どおりであること。
- 新規エラーコード `MODEL_JANG_OSAURUS_REQUIRED` を `ErrorText`（日英）と `anthropic_stream`（`invalid_request_error`）に登録済みであり、`test_v3_2_0_error_contract.py` の `CoordinatorErrorText` 登録漏れ検査が通ること。

### 1-3. 独立レビューの指摘に対する回帰

v5.0.1 実測（実機 `plan_load`）: A（JANG）は `bundled True/False` どちらでも `MODEL_JANG_OSAURUS_REQUIRED` で即失敗（Osaurus 案内、絶対パスなし）、B（Niwaki）は `False` で `MODEL_NEEDS_BUNDLED_LOADER` / `True` で plan 成功（`niwaki-qwen4-exp`）、C（通常 affine モデルの回帰）は pass。

---

## 2. 実機確認手順（必須）

### 2-1. `plan_load` の A/B/C 判定

`Workers/mlx_vlm_worker/loaders.plan(path, bundled_loaders_enabled=...)` で判定を行う（v5.0.1 実測: A・B・C すべて期待どおり）:

| # | 入力 | 期待（v5.0.1 実測） |
|---|---|---|
| 2-1-A | JANGH2 のモデルディレクトリ、`bundled_loaders_enabled=False` / `True` | どちらでも `MODEL_JANG_OSAURUS_REQUIRED` で即失敗し、Osaurus 0.25.24+ 案内を含む（絶対パスなし） |
| 2-1-B | Niwaki のモデルディレクトリ、`bundled_loaders_enabled=False` | `MODEL_NEEDS_BUNDLED_LOADER` で即失敗し、文言に「設定 → モデル → バンドル済みローダーを許可」の有効化経路が含まれる |
| 2-1-C | 同上（Niwaki）、`bundled_loaders_enabled=True` | `MODEL_NEEDS_BUNDLED_LOADER` にならず、同梱ローダ `niwaki-qwen4-exp` で plan 成功（preflight の `loader` 項目が返る） |
| 2-1-D | 通常 affine モデル（回帰） | 従来どおり plan 成功（JANG / bundled 判定に入らない） |

### 2-2. Niwaki は bundled true でロード

| # | 手順 | 期待 |
|---|---|---|
| 2-2 | 設定（`models.bundledLoaders=true`）で Niwaki をロード | ロード成功し、`loadedModels` に現れる。ワーカー側で bundled loader の使用ログが出る |

### 2-3. JANG は数秒で即失敗

| # | 手順 | 期待 |
|---|---|---|
| 2-3 | JANGH2（例: Ornith-1.5-122B-JANGH2 のディレクトリ）をロード | 数秒のうちに `MODEL_JANG_OSAURUS_REQUIRED`＋ Osaurus 0.25.24+ 案内（一括で言えば、重みの深い rotation エラーまで待たない）。メッセージに絶対パスは含まれない |
| 2-4 | 同上を、mlx-lm 側でも確認（エンジン選択） | mlx-lm でも mlx-vlm でも同じ文言で即失敗（supervisor の早期拒否が先に返しても、同じコード・同じ文言になること） |

---

## 3. 手動確認（GUI・実機）

アプリを入れ替えたあと、次を目視で確認します。

| # | 手順 | 期待 |
|---|---|---|
| 3-1 | カタログで JANGH2 の行を見る | オレンジ行で「JANG jangtq2 形式です。このアプリでは実行できません。Osaurus 0.25.24+ で開いてください」「LM Studio・oMLX では実行できません。」が表示される |
| 3-2 | カタログで Niwaki の行を見る | オレンジ行で「このモデルは同梱ローダー（Niwaki qwen4_exp）が必要です。設定 → モデル → バンドル済みローダーを許可を有効にすると読み込めます」が表示される |
| 3-3 | 設定 → モデル → バンドル済みローダーを許可を ON/OFF | OFF（既定）では Niwaki が即失敗、ON ではロード可能になる。説明文「Niwaki（Qwen3.8-Flash-Next qwen4_exp）など同梱ローダーが必要なモデルに使います。」が表示される |
| 3-4 | 言語を英語（English）に切り替え | 3-1・3-2 のバナーが英語表示になる（`JANG jangtq2 format. This app cannot run it. Open it with Osaurus 0.25.24+.` など、日本語のままのキーが無い） |
| 3-5 | 言語を日本語に戻す | 3-1・3-2 の表示に戻る |
| 3-6 | 通常モデル（affine 量子化など）のカタログ行 | JANG / Niwaki のバナーが出ない。ロードも従来どおり |

---

## 4. 確認しなかった範囲（既知の制限）

- JANG ランタイムの内製は中期課題である（次回以降）。本版では JANG は Osaurus 0.25.24+ が必要である。
- Niwaki の長い生成の品質評価は継続中である（v3.2.0 から継続。本版では変更しない）。
- 自動テストの v5.0.1 実測は §1 に記入済み（pytest 1061 passed / 1 skipped / 0 failed、swift test 45 / 0、check_i18n 529 / 0）。`Tests/test_v5_0_1_jang_niwaki.py`（10件）は収録済みである。
