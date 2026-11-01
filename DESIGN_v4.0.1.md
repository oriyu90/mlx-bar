# DESIGN v4.0.1 — ロード中の操作とメモリ判定

## 1. 入力

ユーザー要求: 「ロード中の常駐設定変更・メモリキャッシュ削除・並列数変更・アプリ終了を検証」し、
「最新ランタイム（vlm/lm/omlx）への完全適合を調査検証」したうえで修正（v4.0.1）。

精査（`tasks/TASK-008/PLAN/FINDINGS.md`）: CRITICAL 0 / HIGH 1 / MEDIUM 4 / LOW 3 / INFO 4。

## 2. 設計判断

### D-1 メモリ逼迫は「拒否理由」ではなく「判断材料」（F-01 / HIGH）

`kern.memorystatus_vm_pressure_level` は 1〜4。レベル2は警告で、他アプリのページキャッシュでも
日常的に発生する。旧コードはレベル2で**無条件に全ロードを拒否**し、直前に計算した
`available` と `reserve` を使っていなかった（同一ファイルの他の判定はすべて `>= 4`）。

- レベル4: 無条件拒否（critical）
- レベル2〜3: `available − reserve >= estimate` なら許可
- 拒否時は `detail` に `pressure/available/reserve/estimate/headroom` を含める

OOM を避けるため、緩和は必ず「実測の余裕」との比較を伴う。単なるしきい値変更（2→4）はしない。

### D-2 ロード中のキャッシュ削除は「スキップして即答」（F-02 / MEDIUM）

スロットは `worker.load()` の await **前**に publish されるため、ロード中に clear を投げると
ワーカーの `load()` の後ろに並び、ロード完了まで応答しない（＝v4.0.0 で消した「固まる」症状の再来）。
ロックを取ると clear 自体が数分待つ。よって **`state != "ready"` のスロットはスキップ**し、
`{"skipped": "loading", "modelId": ...}` を即座に返す。memory / disk / paged の3経路と
`_legacy`（プール無効時）を揃える。

### D-3 設定変更はロードと独立（F-06 / INFO の維持）

`settings.write_lock` の取得は `management.py` の1箇所のみで、ロード経路は取らない。
この構造が「100秒のロード中でも設定変更が即座に通る」根拠なので、**回帰テストで固定**する。

### D-4 並列数のラッチは仕様（F-04 / MEDIUM）

`generationConcurrency` は再起動ラッチ、`maxResidentModels` は即時。挙動は正しいが
表示が不親切（既知の制限）。

### D-5 classifier は完全一致で追加（F-08 / LOW）

mlx-vlm 0.7.11 の vision/OCR 系 `model_type` を**完全一致**で登録。部分一致にすると
テキスト専用の `lfm3` まで vision 扱いになり、バッジと engine 選択が壊れる。
`lfm3` / `oxr` は追加しない。

## 3. 変更点

| ファイル | 変更 |
|---|---|
| `workers/model_pool.py` | pressure 判定（レベル定数・余裕比較・診断 detail）、clear の skip（3経路＋legacy） |
| `errors.py` | `detail: dict \| None`（additive） |
| `catalog/classifier.py` | `MLX_VLM_VISION_MODEL_TYPES`（7型・完全一致）、`qwen4_exp` コメント更新 |
| `Tests/load_window.py` | ロード窓ヘルパ（製品コード無変更） |
| `Tests/test_v4_0_1_*.py` | 19 テスト |
| `Tests/test_v2_7_parallel_idle.py`, `test_v2_8_overflow_idle.py` | pressure 意図の更新（2→4、保証内容は維持） |

## 4. 見送り（既知の制限）

- WP-4 ロード中の解放系 UI の事前 disable
- WP-5 `latchedKeys` の公開
- WP-6 unpin 時の profile 再生成抑止
- oMLX の動的契約検証（未インストール）
