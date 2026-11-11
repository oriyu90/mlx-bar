# MLXBar v5.1.0 リリースノート

**1つのモデルを1回だけロードしたまま、複数の要求を同時に生成できるようになりました**（連続バッチ処理・既定で無効）。
あわせて、**API要求でモデルが勝手に常駐し続ける不具合**を修正しました。

v5.1.0 は、同じモデルへの複数の要求を「順番待ち」でも「モデルの複製（レプリカ）」でもなく、
**同じ重みを共有したまま同時に生成**できるようにします（mlx-lm の `BatchGenerator`）。
モデルごとに有効化でき、既定は無効です。有効にしていないモデルの動きは v5.0.2 と同じです。

同時に、一度でも手動でロードしたモデルがアンロードするまで永久に常駐し、
API要求のたびにレプリカが増えていく問題を修正しました。

---

## 何を変えたか

### 連続バッチ処理（既定で無効）

- **1モデル・1ロード・複数セッション**: 有効にしたモデルは、同じ Worker の中で複数の要求を同時に処理します。
  要求ごとに KV キャッシュ・サンプラ・停止条件・トークン上限を独立管理するので、セッション間で出力が混ざりません
- **レプリカとは別機能**: レプリカ方式はモデルを複製してメモリを増やしますが、連続バッチ処理は重みを共有し、
  増えるのは KV キャッシュだけです。バッチ中のモデルはレプリカを増やしません
- **自動フォールバック**: 次の場合は自動的に従来の単一生成へ切り替わります（クライアントから見た応答は同じです）
  - バッチ非対応モデル（ハイブリッド・再帰型キャッシュなど）
  - `seed` 指定、KV キャッシュ量子化、プロンプトキャッシュ、アダプティブメモリ、画像入力を使う要求
- **メモリ安全**: 追加シーケンスごとに同時生成ヘッドルームを計上し、予算を超えるときはバッチに入れず順番待ちにします
- **キャンセルは要求単位**: クライアントの切断や停止は、その要求だけを止めます。生成途中の不具合はエラーとして返し、
  黙って再生成はしません
- **設定**: 設定 → モデル →「連続バッチ処理（実験的）」、または
  `mlxbarctl config set-continuous-batching --model MODEL_ID --enabled true --max-sequences 4`

```json
{"models":{"pool":{"profiles":[
  {"modelId":"MODEL_ID","keepLoaded":true,
   "continuousBatching":{"enabled":true,"maxSequences":4,
                         "prefillBatchSize":2,"prefillStepSize":2048,"fallback":true}}
]}}}
```

### モデル永続化の暴走を修正

- **API要求でモデルが勝手に常駐し続ける**: 一度手動でロードしたモデルは、アンロードするまで
  「固定」として扱われ、待機時間（`idleTTLSeconds`）では解放されませんでした。
  さらに `profiles[].replicas` が残っていると、API要求のたびに背景でレプリカが増えていました。
  修正後は、**明示的にロードして保持したモデル**だけが固定され、API要求で自動ロードしたモデルは
  待機時間で解放されます
- **次回起動時の復元**: API要求で自動ロードしたモデルが、次回起動時に勝手に復活しなくなりました。
  復元されるのは、明示的にロードして保持したモデルだけです
- **レプリカの背景補充**: `keepLoaded: true` のモデルだけが対象です（手動ロードしただけのモデルは増えません）

---

## 制限

- **mlx-vlm（画像対応モデル）は未対応です。** 連続バッチ処理は mlx-lm ランタイムのみです。
  有効にしても「連続バッチ非対応のため単一生成で動作します」と表示され、従来どおり動きます
- **プロンプトキャッシュ・KV量子化・seed指定・アダプティブメモリとは併用しません。** それらを使う要求は
  単一生成で処理されます（速度の最適化より正しさを優先しています）
- **設定は次にモデルをロードしたときから反映されます。** モデルが使用中でも設定は保存できますが、
  切り替えには再ロードが必要です
- **性能はモデルと要求の組み合わせに依存します。** 同じモデルへの同時要求が多いほど効きます。
  1件ずつの利用では単一生成と同じです

## 互換性

- **既定は無効**: 連続バッチ処理を有効にしていないモデルの経路・設定・API 応答は v5.0.2 と同一です
- **API 形状は不変**: OpenAI 互換・Anthropic 互換の SSE、tool calling、reasoning、usage はそのままです
- **設定スキーマは `schemaVersion=1` のまま**。`continuousBatching` は追加キーで、既存の設定ファイルはそのまま読めます
- **意図した挙動変更が1つ**: API要求で自動ロードしたモデルは、次回起動時の復元対象ではなくなりました
  （これが今回直した不具合です）

---

## 検証

- `pytest`: 1140 passed / 1 skipped / 1 failed（既知 flaky の heartbeat 1 件。変更前のコードでも同じ失敗を実測）
- 新規テスト 50 件（`Tests/test_v5_1_0_batch.py` 37 / `Tests/test_v5_1_0_persistence.py` 13）
- `swift build`: warning 0 / error 0、`swift test`: 45 tests / 0 failures、`check_i18n`: 535 keys / 0 missing
- 実機 e2e `scripts/verify-batch-runtime.py`: 全チェック PASS（12 項目。実コーディネーター + 実ワーカー +
  実 mlx-lm モデル。2並列・1 Worker・キャンセル独立・seed フォールバック・無効時の直列）
- 詳細は `DESIGN_v5.1.0.md`（設計）、`TEST_PLAN_v5.1.0.md`（検証）を参照してください

---

## インストール

DMG を開き、`MLXBar.app` を「アプリケーション」へコピーしてください。
このビルドは **ad-hoc 署名**で、Apple の公証は受けていません。初回起動時に「システム設定」→「プライバシーとセキュリティ」からの許可が必要になることがあります。外付けボリューム上のモデルを使う場合は、更新のたびに「リムーバブルボリューム」の許可を求められることがあります（ad-hoc 署名のため）。

SHA-256: `（ビルド後に差し替え）`

---

# MLXBar v5.1.0 Release Notes (English)

**One loaded model can now serve several requests at once** (continuous batching, off by default).
This release also fixes a bug where an API request could make a model stay resident by itself.

v5.1.0 lets multiple requests for the same model generate **at the same time while sharing the
same weights** (mlx-lm's `BatchGenerator`), instead of waiting in line or loading a second copy
(a replica). It is enabled per model and off by default; models without it behave exactly as in v5.0.2.

It also fixes a model that had ever been loaded by hand staying resident until it was unloaded,
with replicas growing in the background on every API request.

---

## What changed

### Continuous batching (off by default)

- **One model, one load, several sessions**: an enabled model serves several requests in the same
  worker. Each request keeps its own KV cache, sampler, stop conditions and token budget, so outputs
  never mix between sessions
- **Separate from replicas**: replicas duplicate the model and its memory; continuous batching
  shares the weights and only adds KV caches. A batched model does not grow replicas
- **Automatic fallback** to single generation (the client sees the same response) for:
  - models that cannot batch (hybrid or recurrent caches, for example)
  - requests that use a seed, KV-cache quantization, the prompt cache, adaptive memory or images
- **Memory safety**: each extra sequence is charged the concurrent-generation head-room; when the
  budget would be exceeded the request waits its turn instead of joining the batch
- **Per-request cancellation**: a disconnect or stop affects only that request. A failure mid-batch is
  reported as an error and never silently regenerated
- **Settings**: Settings → Models → "Continuous batching (experimental)", or
  `mlxbarctl config set-continuous-batching --model MODEL_ID --enabled true --max-sequences 4`

### Model persistence fix

- **A model no longer persists after an API request**: a model loaded by hand used to be treated as
  pinned until it was unloaded (the idle TTL never applied), and with `profiles[].replicas` set the
  background reaper kept adding replicas on every API request. Now only **explicitly loaded and kept**
  models are pinned; a model autoloaded by an API request is released on the idle TTL
- **Restore at startup**: a model autoloaded by an API request is no longer restored at the next start.
  Only models you explicitly loaded and kept are restored
- **Replica top-up**: only `keepLoaded: true` models are topped up in the background

---

## Limitations

- **mlx-vlm (image-capable models) is not supported yet.** Continuous batching is mlx-lm only; enabling
  it shows "Continuous batching unavailable; using single generation" and keeps working as before
- **Not combined with the prompt cache, KV quantization, seeds or adaptive memory.** Requests that use
  them are served by single generation (correctness over speed)
- **Settings take effect the next time the model is loaded**
- **Speedup depends on the workload.** It helps when several requests target the same model; one
  request at a time behaves the same as single generation

## Compatibility

- **Off by default**: models without continuous batching keep v5.0.2's exact path, settings and API responses
- **API shape unchanged**: OpenAI- and Anthropic-compatible SSE, tool calling, reasoning and usage are untouched
- **Settings schema stays `schemaVersion=1`**; `continuousBatching` is an additive key
- **One intended behaviour change**: a model autoloaded by an API request is no longer the model restored
  at the next start (that was the bug this release fixes)

---

## Verification

- `pytest`: 1140 passed / 1 skipped / 1 failed (one known-flaky heartbeat test that also fails on the pre-change code)
- 50 new tests (`Tests/test_v5_1_0_batch.py` 37, `Tests/test_v5_1_0_persistence.py` 13)
- `swift build`: 0 warnings / 0 errors, `swift test`: 45 tests / 0 failures, `check_i18n`: 535 keys / 0 missing
- Live end-to-end `scripts/verify-batch-runtime.py`: all checks PASS (12) (real coordinator, real worker,
  real mlx-lm model: two sequences on one worker, independent cancel, seed fallback, serial when disabled)
- See `DESIGN_v5.1.0.md` (design) and `TEST_PLAN_v5.1.0.md` (verification)

---

## Install

Open the DMG and copy `MLXBar.app` to Applications.
This build is **ad-hoc signed** and not notarized by Apple. On first launch you may need to allow it
under System Settings → Privacy & Security. If your models live on an external volume, macOS may ask
for "Removable Volumes" access again after every update (a consequence of ad-hoc signing).

SHA-256: `(filled in after the build)`
