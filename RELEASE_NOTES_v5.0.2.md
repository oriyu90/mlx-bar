# MLXBar v5.0.2 リリースノート

上げるだけです。v5.0.1 から v5.0.2 へ更新するだけで、Niwaki の退化生成が修正されます。
**既定は無効のまま**で、通常モデルの使い方は v5.0.1 のままです。

v5.0.2 は、Niwaki（Qwen3.8-Flash-Next qwen4_exp）の退化生成を修正します。
mlx-vlm 0.7.12 の batch-invariant decode が bundled ローダー側の compact MoE の remap を迂回するため、
bundled モデルにだけ batch-invariant を無効化して通常 forward へ戻します。
あわせて、modalities のテンソル証拠準拠、ロード失敗時の部分ロード解放、
キャッシュ fingerprint への loaderId 混ぜ込み、bundled への admission strict 強制を行いました。
新規 UI 文言はありません（Python 側の変更のみです）。

---

## 何を変えたか

- **bundled 限定の batch-invariant 無効化**: bundled ロード直後に
  `language_model._supports_batch_invariant_decode` を `False` に差し替え、
  decode を通常 forward へ戻します。非 bundled モデルは batch-invariant 経路のままです
- **modalities のテンソル証拠準拠**: `vision_config` だけを宣言し、vision テンソル 0 件の
  Niwaki を image 対応に誤分類していた問題を修正。テンソル証拠により `["text"]` となり、
  coordinator の分類と一致します（inspect 失敗時は config 判定へフォールバックします）
- **キャッシュ fingerprint への loaderId 混ぜ込み**: bundled / stock ローダー間で
  古い KV / ディスクキャッシュを共有しなくなりました（非 bundled の値は変わりません）
- **bundled の admission strict 強制**: `loaderId` を持つ行は、
  設定が `balanced` でも `strict` で見積もります
- **失敗時の部分ロード解放**: ロード失敗時に半構築モデルのテンソルを即時解放します
  （元のエラーはそのまま返します）

## 制限

- **bundled モデルの生成はやや遅い可能性があります。** bundled モデルは通常 forward へ戻すため、
  非 bundled の batch-invariant 経路より遅い場合があります。性能は本版では測定していません
- **Niwaki は既定で無効です。** 読むには「設定 → モデル → バンドル済みローダーを許可」を有効にしてください（既定は無効）
- **JANG は引き続き Osaurus が必要です。** JANGH2（jangtq2）形式は MLXBar / MLX LM / MLX VLM / LM Studio / oMLX では実行できません（v5.0.1 から変更なし）

## 互換性

- **通常モデルの回帰なし**: batch-invariant 無効化・fingerprint 混ぜ込み・admission strict はすべて
  bundled（`loaderId` あり）の行にだけ効き、非 bundled の経路・値は v5.0.1 と同一です
- **設定スキーマは `schemaVersion=1` のまま**。`models.bundledLoaders` の既定は `false` のままです
- **カタログ分類版は `"4"` のまま**です（v5.0.2 は分類規則を変更しないため再走査は不要です）
- **新規 UI 文言はありません**。日英 UI の表示は v5.0.1 のままです
- 詳細は `DESIGN_v5.0.2.md`（設計）、`TEST_PLAN_v5.0.2.md`（検証）を参照してください

---

## 検証

- `pytest` 1091 passed / 1 skipped / 0 failed（`cd Coordinator && .venv/bin/python -m pytest ../Tests -q -p no:randomly`）
- `check_i18n` 529 keys / 0 missing
- 新規テスト `Tests/test_v5_0_2_niwaki_regressions.py`（30件）
- Swift 側の変更はありません（Python 側の変更のみで Swift / UI 文言の変更なし）

---

## インストール

DMG を開き、`MLXBar.app` を「アプリケーション」へコピーしてください。
このビルドは **ad-hoc 署名**で、Apple の公証は受けていません。初回起動時に「システム設定」→「プライバシーとセキュリティ」からの許可が必要になることがあります。外付けボリューム上のモデルを使う場合は、更新のたびに「リムーバブルボリューム」の許可を求められることがあります（ad-hoc 署名のため）。

SHA-256: `aa397d0f9b37ed4671a9ead48f7248054e7ccaac8eb0174bae4efac83a68e71b`

---

# MLXBar v5.0.2 Release Notes (English)

Just upgrade. Updating from v5.0.1 to v5.0.2 fixes degraded Niwaki generation.
**They are off by default,** and normal-model usage is unchanged from v5.0.1.

v5.0.2 fixes degraded generation for Niwaki (Qwen3.8-Flash-Next qwen4_exp).
mlx-vlm 0.7.12's batch-invariant decode bypasses the bundled loader's compact MoE remap,
so batch-invariant decode is disabled for bundled models only, restoring the normal forward.
It also makes `modalities` follow tensor evidence, releases partial loads on failure,
mixes `loaderId` into the cache fingerprint, and forces the strict admission estimate for bundled models.
There are no new UI strings (Python-side changes only).

### What changed

- **Bundled-only batch-invariant disable**: right after a bundled load,
  `language_model._supports_batch_invariant_decode` is replaced so decode returns `False`
  and falls back to the normal forward. Non-bundled models keep the batch-invariant path
- **Tensor-evidence modalities**: Niwaki, which declares a `vision_config` but ships zero vision tensors,
  is no longer mislabelled as image-capable; it now reads as `["text"]`, matching the coordinator classifier
  (config-only reading is kept as a fallback if inspect fails)
- **Cache fingerprint now includes loaderId**: bundled and stock loaders no longer share KV / disk cache namespaces
  (non-bundled fingerprint values are unchanged)
- **Strict admission for bundled models**: rows carrying a `loaderId` always use the `strict` estimate,
  even when `memoryAdmission` is `balanced`
- **Partial loads are released on failure**: on a load error the half-built tensors are freed immediately
  (the original error is returned unchanged)

### Limitations

- **Bundled generation may be somewhat slower.** Bundled models go through the normal forward,
  so they may be a little slower than the non-bundled batch-invariant path. Performance is not measured in this release
- **Niwaki is off by default.** Enable Settings → Models → Bundled loaders to load it
- **JANG still needs Osaurus.** JANGH2 (jangtq2) cannot be run by MLXBar / MLX LM / MLX VLM / LM Studio / oMLX (unchanged from v5.0.1)

### Compatibility

- No regression for normal models: the batch-invariant disable, fingerprint mixing, and strict admission
  apply only to bundled rows (`loaderId` set); non-bundled paths and values are identical to v5.0.1
- Settings schema stays at `schemaVersion` 1; `models.bundledLoaders` stays off by default
- The catalog classifier version stays at `"4"` (v5.0.2 changes no classification rule, so no rescan is needed)
- There are no new UI strings. JA/EN UI text is unchanged from v5.0.1
- See `DESIGN_v5.0.2.md` (design) and `TEST_PLAN_v5.0.2.md` (verification) for details

### Verification

- pytest 1091 passed / 1 skipped / 0 failed, check_i18n 529 keys / 0 missing. New tests: `Tests/test_v5_0_2_niwaki_regressions.py` (30 tests). There are no Swift-side changes (Python-side changes only; no Swift / UI text changes).

### Install

Open the DMG and copy `MLXBar.app` to Applications. This build is **ad-hoc signed and not notarised**; on first launch macOS may ask you to allow it in System Settings → Privacy & Security.

SHA-256: `aa397d0f9b37ed4671a9ead48f7248054e7ccaac8eb0174bae4efac83a68e71b`
