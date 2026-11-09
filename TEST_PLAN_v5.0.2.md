# TEST PLAN v5.0.2 — Niwaki 退化生成の修正

対象: MLXBar v5.0.2（build 72）。v5.0.1 からの修正版（Python 側の変更のみで Swift / UI 文言の変更なし）。
設計: [`DESIGN_v5.0.2.md`](DESIGN_v5.0.2.md)

---

## 1. 自動テスト（必須・全パス）

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly
python3 scripts/check_i18n.py
```

| 項目 | v5.0.2 実測 |
|---|---|
| pytest | 1091 passed / 1 skipped / 0 failed（`cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly`） |
| check_i18n | 529 keys / 0 missing（`python3 scripts/check_i18n.py`） |

Swift 側の変更が無いため、swift build / swift test の v5.0.2 実測値は本計画では記録しません
（捏造防止のため、既存の値の v5.0.2 への流用もしません）。

### 1-1. 新規テスト（`Tests/test_v5_0_2_niwaki_regressions.py`、30件）

本テストファイルは収録済みである（30件の単体テストを含む pytest 全体で 1091 passed / 1 skipped / 0 failed）。

| クラス（実名） | 件数 | 確認内容 |
|---|---|---|
| `BatchInvariantDisabledTests` | 6 | adapter が `_supports_batch_invariant_decode` 等に触れること、helper `_disable_batch_invariant_for_bundled` が bundled のみ無効化すること、非 bundled は no-op、`language_model` 欠落でも例外にならないこと、bare な language_model にも callable を作ること、`load()` が bundled 分岐で helper を呼ぶこと |
| `ModalitiesVisionInspectTests` | 5 | Niwaki 合成（0 vision テンソル）で `vision.inspect` が `vision=False` / `readable=True` であること、分類器の `modalities == ["text"]`・`capabilities.vision == False`・`loaderId == niwaki-qwen4-exp`・engine `mlx-vlm` であること、inspect 由来と分類器の modalities が一致すること、vision テンソルあり（positive control）では両方とも `["text", "image"]` であること、adapter が `from common import vision` / `vision.inspect` を参照すること |
| `FingerprintLoaderIdTests` | 5 | loaderId あり / なしで fingerprint が変わること（64 hex のまま異なる）、同一 loaderId で決定性、異なる loader（niwaki vs jang）で差分、既定 `None` と明示 `None` の一致、ランタイム版を変えると loaderId ありでも変わること |
| `AdmissionStrictForBundledTests` | 5 | `balanced` で bundled は `strict`（1.35x）、非 bundled は `balanced`（1.15x）であること、snake-case `loader_id` でも `strict` 強制であること、`strict` 設定では loader 有無で一致すること、`loaderId=None` は設定どおり（`balanced`）であること |
| `BundledLoadersParityTests` | 7 | 既定が `false`（store / public どちらも）、`true` / `false` 更新が受け入れられ永続化されること、非 bool（`"yes"` / `"true"` / `1` / `0` / `None` / `[]` / `{}`）が `ValueError` であること、`nested_patch("models.bundledLoaders", ...)` が GUI の PUT 形と一致すること、CLI `config set` が `PUT /api/v1/settings` に同形を送ること、GUI ペイロードがサーバに受け入れられること、`config get` が同じキーを含むこと |
| `CatalogVersionPinnedTests` | 2 | `CATALOG_CLASSIFIER_VERSION == "4"`（v5.0.1 で上げたまま。v5.0.2 は分類規則を変更しない）、`schemaVersion == 1` |

### 1-2. 回帰（既存テスト）

- 非 bundled モデルが F-1 〜 F-4 のうちどれにも入らず、従来の decode・modalities・fingerprint・見積りであること
  （§1-1 の positive control / 非 bundled 分岐がこれを担保します）。
- `bundledLoaders` の既定 `false` と CLI / GUI 同形は §1-1 の `BundledLoadersParityTests` で担保します。

---

## 2. 実機確認手順（必須）

前提: `models.bundledLoaders == true`（Niwaki のロードには必須）。
ワーカー log で `Bundled loader enabled: niwaki-qwen4-exp` と
`Batch-invariant decode disabled (CompactMasked bypass in mlx-vlm 0.7.12)` の
WARNING が出ることを、ロードの毎回確認します。

### 2-1. `plan` / ロードの A/B/C 判定

| # | 入力 | 期待 |
|---|---|---|
| 2-1-A | Niwaki のモデルディレクトリ（bundled ローダー対象） | ロード成功し、`loadedModels` に現れる。ワーカー側に bundled ローダー使用 + batch-invariant 無効化の WARNING が出る |
| 2-1-B | Niwaki のモデルディレクトリ、C（対象外） | `modalities` が `["text"]` のみであること（image 対応に誤分類されない）。分類器の `capabilities.vision == False` と一致すること |
| 2-1-C | 通常 affine 量子化モデル（回帰） | 従来どおりロード・生成できること。batch-invariant 無効化の WARNING が出ないこと（非 bundled は batch-invariant 経路のまま）。`modalities` が従来どおりであること |

### 2-2. API 各経路（退化の通常化確認）

| # | 経路 | 手順 | 期待 |
|---|---|---|---|
| 2-2-A | OpenAI Chat Completions `POST /v1/chat/completions` | Niwaki に短い日本語の prompt で生成させる | `!!!!` のような重複・崩壊ではなく通常の文章で返ること（外部 API 退化の通常化） |
| 2-2-B | completions `POST /v1/completions` | 同上（completions 形式） | こちらも通常の文章で返ること |
| 2-2-C | Anthropic `POST /anthropic/...`（公開面） | 同上の prompt で生成させる | こちらも通常の文章で返ること |
| 2-2-D | 非 bundled 通常モデル | 同じ API 経路（2-2-A）で生成させる | 従来どおり正常生成すること（回帰なし） |

---

## 3. 手動確認（GUI・実機）

アプリを入れ替えたあと、次を目視で確認します。

| # | 手順 | 期待 |
|---|---|---|
| 3-1 | カタログで Niwaki の行を見る | `modalities` が text のみであること（image 対応として表示されない）。v5.0.1 の Niwaki バナー（同梱ローダーの有効化経路）はそのまま表示される |
| 3-2 | Niwaki 行で画像入力を確認 | 画像ボタンが無効であること（text のみのため） |
| 3-3 | 設定 → モデル → バンドル済みローダーを許可を ON/OFF | OFF（既定）では `MODEL_NEEDS_BUNDLED_LOADER` で即失敗、ON では §2-1-A のとおりロードできること。説明文は v5.0.1 のままであること |
| 3-4 | 通常モデル（affine 量子化など）のカタログ行 | JANG / Niwaki のバナーが出ない。ロードも従来どおりであること |
| 3-5 | 言語を英語（English）／日本語に切り替え | v5.0.1 のバナーの日英表示がそのままであること（新規 UI 文言は無い） |

---

## 4. 確認しなかった範囲（既知の制限）

- 実機の生成品質（ §2-2 の `!!!!` 通常化）は本計画の期待値として明記するが、
  実測自体は別タスクである（本計画では手順のみを定める）。
- bundled モデルの生成は通常 forward のため、非 bundled の batch-invariant 経路より
  やや遅い可能性があります（性能は本版では測定しません）。
- Niwaki の長い生成の品質評価は継続中である（v3.2.0 から継続。本版では変更しない）。
- 自動テストの v5.0.2 実測は §1 に記入済み（pytest 1091 passed / 1 skipped / 0 failed、check_i18n 529 / 0）。`Tests/test_v5_0_2_niwaki_regressions.py`（30件）は収録済みである。
