# TEST PLAN v4.0.0 — ロード競合・設定整合性・表示の正確性

対象: TASK-007。`RELEASE_NOTES_v4.0.0.md` の主張を実行可能な手順に対応づける。

## 1. 自動テスト

```sh
cd Coordinator && ./.venv/bin/python -m pytest ../Tests -q
swift test
./Coordinator/.venv/bin/python scripts/check_i18n.py
```

| ID | 期待 |
|---|---|
| T-1 | `pytest` = **927 passed / 0 failed / 1 skipped**（skip は環境依存の1件） |
| T-2 | `swift test` = 29 tests / 0 failures |
| T-3 | `check_i18n` = 456 キー欠落 0 |
| T-4 | 新規テスト 47 件（pool 6 / preload・settings 29 / verification 7 / rework1 5）が全て pass |

### 回帰テストとして機能していることの確認

修正前のコードに戻した複製（`/tmp` に HEAD のファイルを戻す）で、以下が fail することを実測:

| 対象 | 修正前 | 修正後 |
|---|---|---|
| `test_v4_0_0_pool_concurrency.py` | 5 / 6 fail | 6 pass |
| `test_v4_0_0_preload_settings.py` | 20 / 29 fail | 29 pass |
| `test_v4_0_0_rework1.py` | 4 / 5 fail | 5 pass |

## 2. 手動検証（実機）

### V-1 ロード中のアンロードで固まらない（WP-5）

35GB 級のモデルをコールドロードし、読み込み中（`/api/v1/status` の `loadingModel` が非 null）に
メニューバーの「アンロード」を押す。

期待: **5 秒以内**に「モデルを読み込み中です。完了後にもう一度お試しください」が出る。
アプリは操作可能なまま。強制終了が必要にならない。

### V-2 常駐設定を変えても設定が壊れない（WP-4 / WP-7）

```sh
# 別のモデルを読み込み中に、常駐の pin とメモリ上限を同時に変更する
curl -s -X PUT -H 'Content-Type: application/json' -d '{"keepLoaded":true}' \
  --unix-socket "$SUP/control/coordinator.sock" \
  "http://localhost/api/v1/settings/models/pool/profiles/<ID>" >/dev/null &
curl -s -X PUT -H 'Content-Type: application/json' -d '{"models":{"pool":{"profiles":[{"modelId":"<ID>","keepLoaded":true,"maxMemoryGB":48}]}}}' \
  --unix-socket "$SUP/control/coordinator.sock" http://localhost/api/v1/settings >/dev/null &
wait
```

期待: 両方の変更が最終状態に残る（片方が消えない）。ロック競合時は 409 `SETTINGS_CONFLICT`
（500 ではない）。再起動しても設定が維持される。

### V-3 LAN 到達性と認証（WP-10 の R-08）

別ホスト（同一 LAN）から `http://<Mac の LAN IP>:11435/v1/models` を叩く。

期待: `Authorization` 無しで **401**、正しい鍵で 200。`/v1/models` は無認証にならない。

### V-4 常駐モデルの失敗表示が復旧する（WP-2）

1. 外付けボリュームの許可を外した状態で起動 → `preload.state=failed`（`MODEL_VOLUME_UNREADABLE`）
2. 許可を与える（システム設定 → リムーバブルボリューム）
3. **再起動せずに** 30 秒待つ

期待: 自動再試行が走り、メニューバーの表示が「読み込み中…」→ 消える（`loaded`）。
`/api/v1/status` の `preload.state` が `loaded` になる。

### V-5 手動ロード後の誤認識が無いこと（WP-2）

常駐モデルの preload が失敗している状態で、**同じ**常駐モデルを手動でロードする。

期待: `preload.state` が `loaded` になる（失敗の履歴は `reason` に残るが、表示は現在を語る）。
**別モデル**をロードした場合は `failed` のまま（混同しない）。

### V-6 終了時に新しいロードが始まらない（REVIEW R-01）

常駐モデルをコールドロード中に MLXBar を終了する。

期待: 終了が完了し、`pgrep -f mlx_vlm_worker` に孤児が残らない。
ログに `終了処理中` / `ENGINE_SHUTTING_DOWN` が出ても、新しいワーカーは起動しない。

## 3. 既知の制限（テストで固定済み）

- 常駐数上限を増やしても、既存の非駐在モデルは自動昇格しない（次のロードで反映）
- ロード中の unpin は admission を再評価しない
- `status()` は観測時に内部状態を修復する（GET の副作用）
- `/status` と `/health` が evicting 中の数秒だけ食い違う
