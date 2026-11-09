# MLXBar v5.0.1 リリースノート

上げるだけです。v5.0.0 から v5.0.1 へ更新するだけで、JANG・Niwaki の案内改善が効きます。
**既定は無効のまま**で、通常モデルの使い方は v5.0.0 のままです。

v5.0.1 は、JANGH2（jangtq2）形式と Niwaki（Qwen3.8-Flash-Next qwen4_exp）の取り扱いを改善します。
JANGH2 は MLXBar / MLX LM / MLX VLM / LM Studio / oMLX のいずれでも実行できないため、
Osaurus 0.25.24+ での実行を案内して数秒で即失敗するようになりました。
Niwaki などの非標準形式は既存の同梱ローダーで読み込みでき、エラー文言とカタログバナーで有効化の経路が案内されます。
カタログバナーは日本語・英語の両言語対応です。

---

## 何を変えたか

- **JANG 検出（3条件）**: `jang_config.json`（`format == "jangtq2"` / `weight_format` が `jangtq` を含む）、
  `config.json` の `quantization.mode == "jangtq2"`（再帰）、`osaurus.json` の存在。
  該当行は `jang-osaurus-required` として、全 runtime が `unsupported` になります
- **ロード前失敗**: JANG は深い読み込み（rotation エラー）まで行かず、
  `MODEL_JANG_OSAURUS_REQUIRED` で Osaurus 0.25.24+ の案内を添えて即失敗します
- **Niwaki の案内改善**: ローダ自体は既存のもので、既定は無効のまま。有効化経路
  （設定 → モデル → バンドル済みローダーを許可）がエラー文言とカタログバナーで案内されます
- **カタログ分類版 4**: 既存インストールでも起動時に再走査され、JANGH2 の行が更新されます

## 制限

- **JANG は Osaurus が必要です。** JANGH2（jangtq2）形式は MLXBar / MLX LM / MLX VLM / LM Studio / oMLX では実行できません。
  JANG の実行には Osaurus 0.25.24+（または対応する MLX Studio / jang-tools）を使ってください。
  JANG ランタイムの内製は中期課題として記録しており、本版では実行できません
- **Niwaki は既定で無効です。** 読むには「設定 → モデル → バンドル済みローダーを許可」を有効にしてください（既定は無効）

## 互換性

- **通常モデルの回帰なし**: 通常の affine 量子化モデル（例: qwen3_5_moe）などは JANG 判定に一致しません
- **設定スキーマは `schemaVersion=1` のまま**。`models.bundledLoaders` の既定は `false` のままです
- **JANG 用の新規エラーコード `MODEL_JANG_OSAURUS_REQUIRED` を設けました**。`ErrorText.swift` に日英登録し、`api/anthropic_stream.py` に `invalid_request_error` の分類を追加しています。Niwaki は従来の `MODEL_NEEDS_BUNDLED_LOADER` を使います
- 詳細は `DESIGN_v5.0.1.md`（設計）、`TEST_PLAN_v5.0.1.md`（検証）を参照してください

---

## 検証

- `pytest` 1061 passed / 1 skipped / 0 failed（`cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly`、flaky 修正後の実測）
- `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift build` パス（警告のみ）、同 `DEVELOPER_DIR` の `swift test` 45 tests / 0 failures
- `check_i18n` 529 keys / 0 missing
- 実機 `plan_load`: JANG は `bundled True/False` どちらでも `MODEL_JANG_OSAURUS_REQUIRED`、Niwaki は `False` で `MODEL_NEEDS_BUNDLED_LOADER` / `True` で plan 成功、通常モデルは回帰なし

---

## インストール

DMG を開き、`MLXBar.app` を「アプリケーション」へコピーしてください。

このビルドは **ad-hoc 署名**で、Apple の公証は受けていません。初回起動時に「システム設定」→「プライバシーとセキュリティ」からの許可が必要になることがあります。外付けボリューム上のモデルを使う場合は、更新のたびに「リムーバブルボリューム」の許可を求められることがあります（ad-hoc 署名のため）。

SHA-256: `10471484fbfe7b08784aa2401c1a8c6237afe8279b3d771ad082649166754a84`

---

# MLXBar v5.0.1 Release Notes (English)

Just upgrade. Updating from v5.0.0 to v5.0.1 enables the JANG/Niwaki guidance improvements.
**They are off by default,** and normal-model usage is unchanged from v5.0.0.

v5.0.1 improves how JANGH2 (jangtq2) and Niwaki (Qwen3.8-Flash-Next qwen4_exp) are handled.
JANGH2 cannot be run by MLXBar / MLX LM / MLX VLM / LM Studio / oMLX, so it now fails fast (within seconds) with guidance to use Osaurus 0.25.24+.
Niwaki and similar non-standard formats can be loaded with the existing bundled loader; the error text and the catalog banner show how to enable it.
The banners are available in both English and Japanese.

### What changed

- **JANG detection (3 conditions)**: `jang_config.json` (`format == "jangtq2"` / `weight_format` containing `jangtq`), `config.json` `quantization.mode == "jangtq2"` (recursive), or `osaurus.json` present. Matching rows are marked `jang-osaurus-required` with every runtime `unsupported`
- **Fail before load**: JANG no longer dies deep in weight loading (rotation error); it returns the new `MODEL_JANG_OSAURUS_REQUIRED` (registered in JA/EN and mapped to `invalid_request_error`) with Osaurus 0.25.24+ guidance
- **Niwaki guidance**: the loader itself is unchanged and off by default; the error text and catalog banner now point to Settings → Models → Bundled loaders
- **Catalog classifier v4**: existing installs rescan at startup, so JANGH2 rows are updated automatically

### Limitations

- **JANG needs Osaurus.** JANGH2 (jangtq2) cannot be run by MLXBar / MLX LM / MLX VLM / LM Studio / oMLX. Use Osaurus 0.25.24+ (or a compatible MLX Studio / jang-tools) to run JANG. Building the JANG runtime in is a recorded medium-term task and is not included in this version
- **Niwaki is off by default.** Enable Settings → Models → Bundled loaders to load it

### Compatibility

- Normal affine-quantized models (e.g. qwen3_5_moe) never match the JANG detection, so there is no regression
- Settings schema stays at `schemaVersion` 1; `models.bundledLoaders` stays off by default
- JANG gains the new `MODEL_JANG_OSAURUS_REQUIRED` code (JA/EN in `ErrorText`, `invalid_request_error` in `anthropic_stream`); Niwaki reuses `MODEL_NEEDS_BUNDLED_LOADER`

### Verification

- pytest 1061 passed / 1 skipped / 0 failed, `DEVELOPER_DIR` swift build pass, swift test 45 tests / 0 failures, check_i18n 529 keys / 0 missing. On-device `plan_load`: JANG fails with `MODEL_JANG_OSAURUS_REQUIRED` either way, Niwaki fails with `MODEL_NEEDS_BUNDLED_LOADER` when off and plans successfully when on, no regression on normal models

### Install

Open the DMG and copy `MLXBar.app` to Applications. This build is **ad-hoc signed and not notarised**; on first launch macOS may ask you to allow it in System Settings → Privacy & Security.

SHA-256: `10471484fbfe7b08784aa2401c1a8c6237afe8279b3d771ad082649166754a84`
