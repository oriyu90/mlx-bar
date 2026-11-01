# TEST PLAN v4.0.1 — ロード中の操作・メモリ判定・ランタイム適合

## 1. 自動テスト

```sh
cd Coordinator && ./.venv/bin/python -m pytest ../Tests -q    # 947 passed / 0 failed / 1 skipped
./Coordinator/.venv/bin/python scripts/check_i18n.py          # 456 keys 欠落 0
```

新規 19 件: `test_v4_0_1_pressure_gate.py`（5）／`test_v4_0_1_load_window.py`（7）／
`test_v4_0_1_classifier.py`（7）。

回帰の実証: 修正前のコードに戻すと **9 failed / 3 passed**
（WP-0 の 5 件 = レベル2/3 で 503 になること、WP-3 の 4 件 = ロード中スロットへの clear）。

## 2. 手動検証

### V-1 メモリ逼迫時でもロードできる（F-01）
```sh
sysctl kern.memorystatus_vm_pressure_level   # 2 以上であることを確認
# 小さなモデルをロード
curl -s -X POST -H 'Content-Type: application/json' -d '{}' \
  --unix-socket "$SUP/control/coordinator.sock" \
  "http://localhost/api/v1/models/<SMALL_MODEL_ID>/load" | head -c 300
```
期待: 200 でロード開始。**修正前は 503 `MEMORY_PRESSURE`（0.04秒）**。
レベル4 の環境では 503 のままでよい（`detail.pressure=4` が付く）。

### V-2 ロード中の常駐設定変更（F-06）
100秒級のロード中に pin / unpin / `maxMemoryGB` を変更。
期待: 即座に 200、`config.json` に永続化、ロードは完走、`status` と一致。

### V-3 ロード中のキャッシュ削除（F-02）
```sh
curl -s -X POST --unix-socket "$SUP/control/coordinator.sock" \
  http://localhost/api/v1/prompt-cache/memory/clear
curl -s -X POST --unix-socket "$SUP/control/coordinator.sock" \
  http://localhost/api/v1/prompt-cache/disk/clear
```
期待: **1秒以内に 200**、本文に `"skipped": "loading"`。ロードは完走、ワーカー生存。
修正前はロード完了まで返らない（または失敗）。

### V-4 ロード中の並列数変更（F-04）
`maxResidentModels` を 2→4 に変更。
期待: 200、`status.modelPool.maxResidentModels` が即 4。`generationConcurrency` は
`configuredGenerationConcurrency` に反映され `restartRequired: true`（既知の制限）。

### V-5 ロード中の終了（WP-5 of v4.0.0）
ロード中に SIGTERM。期待: 2秒以内に終了、`pgrep -f mlx_vlm_worker` に孤児なし、
`control/coordinator.sock` が削除され、再起動して正常にロードできる。

### V-6 ランタイム適合（確認のみ）
- 導入済みスロットで `mlx_lm.load` / `mlx_lm.stream_generate` / `make_prompt_cache` /
  `make_sampler` が解決すること（実測済み）。
- mlx-vlm 0.7.11 は未導入でも、クラシファイアが新モデル型を vision と判定すること
  （`test_v4_0_1_classifier.py`）。
- oMLX は未インストールのため `pytest -k omlx`（31 passed）まで。

## 3. 既知の制限（テストで固定していない）
ロード中の解放 UI の disable／`latchedKeys` の公開／clear skip の GUI 表示／
oMLX の動的契約／niwaki の 0.7.11 実ロード。
