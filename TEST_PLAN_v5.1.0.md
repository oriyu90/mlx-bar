# TEST PLAN v5.1.0 — 連続バッチ処理とモデル永続化の修正

> 対象: MLXBar v5.1.0（build 73）。設計は `DESIGN_v5.1.0.md`。
> 実測値はこの文書の「§4 実測結果」に記録します。

---

## 1. 自動テスト（`pytest`）

```sh
cd Coordinator && .venv/bin/python -m pytest ../Tests -q
```

| ファイル | 件数 | 何を固定するか |
|---|---|---|
| `Tests/test_v5_1_0_batch.py` | 37 | スケジューラ（2 シーケンス同時・要求単位キャンセル・`length`・全体失敗で再試行しない・能力判定・shutdown・プロンプトキャッシュの再利用/保存/非接頭辞の拒否/保存失敗の無害化）、ワーカーの経路選択（非対応/対応/フォールバック/`stream_batched` のシグネチャ）、アダプタの拒否規則（seed・KV量子化・画像・アダプティブメモリ）、プールのバッチレーン（レプリカを作らない・既定無効・上限・`generationConcurrency` 飽和時の待機・環境変数・status）、設定検証 |
| `Tests/test_v5_1_0_persistence.py` | 13 | API 自動ロードが pin しない・常駐枠を消費しない・TTL で解放される、手動ロードは pin する、reaper が `session_pinned` をトップアップしない、`keepLoaded` は補充する、未知の origin は一時扱い、起動時復元（API だけでは何も復元しない・明示ロードは一時復元・`keepLoaded` は固定復元） |
| `Tests/test_model_pool.py` | 既存 | 固定 `sleep` を条件待ちに変更（アサーションは不変） |
| `Tests/test_openai_tools.py` | 既存 | API 要求が `last_loaded_model_id` を書かないこと（`last_api_model_id` に書く） |
| `Tests/test_v3_1_0_startup.py` / `test_v3_3_3_resident_preload.py` / `test_v4_0_0_preload_settings.py` | 既存 | preload の fake が `origin=` を受け取る |

### 1.1 既知の flaky（本版の変更と無関係・HEAD でも再現）

- `Tests/test_worker_server.py::test_buffered_tool_generation_still_emits_heartbeats`
  — 0.10 秒の heartbeat と 0.05 秒間隔のチャンクの競合。**v5.1.0 の変更前（HEAD `2133ad3`）の
  コードでも 3 回中 2 回失敗**することを実測で確認済み（`/tmp` の worktree で実行）。
- `Tests/integration/test_coordinator_e2e.py` の一部 — 実コーディネーター起動待ち 5 秒が、
  このマシンで MLXBar 本体（v5.0.2）が常駐している状態では間に合わないことがある。
  変更前のコードでも同じ失敗を確認済み。

---

## 2. 実機 e2e（`scripts/verify-batch-runtime.py`）

実コーディネーター（`python -m mlxbar.main --home <tmp>`）＋実 mlx-lm ワーカー＋実モデルで、
公開 API（TCP）と管理 API（UDS）を通す。モデルは小さい MLX モデルを指定します。

```sh
python3 scripts/verify-batch-runtime.py --model /path/to/small-mlx-model
```

| # | 確認内容 | 期待 |
|---|---|---|
| 2-1 | `continuousBatching` を有効化してロード | `loadedModels[].continuousBatching.capable == true` |
| 2-2 | 2 リクエストを同時に送る | 両方応答。`batchActiveSequences` のピークが 2。常駐モデル数は 1（レプリカなし） |
| 2-3 | 1 件目が終わる前に 2 件目が生成を始める | サーバー側で 2 シーケンスが同時にアクティブ |
| 2-4 | 片方を切断（停止ボタン相当） | 残りは正常完了。キャンセル後にアクティブなシーケンスが残らない |
| 2-5 | `seed` 指定の要求 | 単一生成へフォールバックして応答 |
| 2-6 | バッチ無効に戻して 2 リクエスト | 両方応答し、2 件目は 1 件目の完了を待つ（v5.0.2 と同じ） |

実測で分かった注意点（スクリプトに反映済み）:

- `temperature: 0` の要求は coordinator が `seed: 0` を付けるため、**バッチ対象外**です。
  バッチを試すには温度を 0 以外にします。
- 一時ホームでワーカーを起動するとき、ランタイム slot をコピーすると初回 import が
  5 秒の起動待ちを超えることがあります（`MLXBAR_WORKER_STARTUP_SECONDS` で延長）。

---

## 3. 手動確認（GUI・日英）

1. 設定 → モデル →「連続バッチ処理（実験的）」でモデルを有効化 → 同時シーケンス数を変更 →
   設定を開き直して値が保持されていること。英語表示で文言が英語になること。
2. メニューバーのモデル行: バッチ中に「連続バッチ: N/M 並列」、非対応モデルを有効にしたときは
   「連続バッチ非対応のため単一生成で動作します」が出ること。アイドル時は行が増えないこと。
3. モデルをロードし直すと、設定が反映されること（ロード前の変更は次のロードから）。
4. **永続化**: API からだけモデルを使い、`idleTTLSeconds` 経過後に解放されること。
   メニューバーで手動ロードしたモデルは固定され、アンロードするまで残ること。
5. アプリ再起動: API で自動ロードしただけのモデルが復活しないこと。手動ロード（または
   `keepLoaded`）したモデルは復活すること。

---

## 4. 実測結果（v5.1.0）

環境: macOS / Apple Silicon、`Coordinator/.venv`（Python 3.14）、mlx-lm 0.32.2、mlx-vlm 0.7.12。

| 項目 | 結果 |
|---|---|
| `pytest`（e2e を除く全件） | 1140 passed / 1 skipped / 1 failed（既知 flaky の heartbeat。§1.1） |
| `swift build --disable-sandbox` | Build complete、warning 0 / error 0 |
| `swift test`（`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`） | 45 tests / 0 failures |
| `check_i18n` | 535 keys / 0 missing |
| `scripts/verify-batch-runtime.py` | **全チェック PASS**（2-1〜2-6、12 項目） |

実機 e2e の実測値（SmolLM2-135M-Instruct-8bit）:

- 2 件同時: 両方応答、`batchActiveSequences` ピーク 2、常駐モデル 1。
- バッチ無効時: 2 件目は 1 件目の完了後に開始（直列）。
- seed 指定: 単一生成で応答。
- 切断: 残りの要求は正常完了、キャンセル後にアクティブ 0。

### 実装中に見つけて直した不具合（実機で初めて判明）

| 症状 | 原因 | 修正 |
|---|---|---|
| 全バッチ要求が `TypeError: missing 'prompt'` | `stream_batched` のシグネチャが選択側と不一致 | シグネチャを合わせ、プロンプトはアダプタ内で描画。単体テストで固定 |
| バッチ要求が空文字で完了 | 単一経路のキャッシュ型を `BatchGenerator` に渡し、マージできず停止トークンだけが返った | バッチ可能なキャッシュ型へ載せ替え、拒否時は冷たいプレフィルへ |
| 停止トークンだけの要求が「停止」に見える | 停止トークンを出力しない際にイベントを出していなかった | `token_progress` を出す |
| ワーカーの生成失敗がログに残らない | 例外の traceback を記録していなかった | traceback 付きで記録 |

---

## 5. 未実施・制限

- mlx-vlm（画像対応モデル）の連続バッチ処理は未実装（設計 §1.1）。GUI に理由を表示します。
- 長い会話でのプロンプトキャッシュのヒット率・速度は未計測。
- バッチと KV キャッシュ量子化の併用は未対応（量子化時は単一生成）。
- 実機の GUI 目視（日英）は §3 の手順で確認してください。
