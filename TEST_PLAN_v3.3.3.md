# TEST PLAN v3.3.3 — 常駐モデルの起動時 preload

対象: TASK-005。`RELEASE_NOTES_v3.3.3.md` の主張を、実行可能な手順と期待結果に対応づける。

## 1. 自動テスト

```sh
cd Coordinator && ./.venv/bin/python -m pytest ../Tests -q
swift test
./Coordinator/.venv/bin/python scripts/check_i18n.py
```

| ID | 期待 |
|---|---|
| T-1 | `Tests/test_v3_3_3_resident_preload.py` 26 件が全て pass |
| T-2 | 全体で 862 passed / 2 failed（`test_lan_listener_switch_keeps_token_required` と `test_disconnect_stops_the_generation_immediately`、いずれも環境依存の既知 flaky）/ 1 skipped |
| T-3 | `swift test` 28 tests / 0 failures |
| T-4 | `check_i18n` 454 キー欠落 0 |

### 新規テストの内容

| クラス | 固定する内容 |
|---|---|
| `PreloadLoggingTests` | 停止ゲート中／常駐対象なし／モデル不在／成功 の各判断がログに出る。モデル不在は `failed` + `RESIDENT_MODEL_MISSING` |
| `ResidentAutoloadRearmTests` | 常駐設定がゲートを解除する。無関係な設定書き込み・却下されたプロファイル・unpin は解除しない。全アンロードは従来どおりゲートを立てる |
| `CatalogScanGuardTests` | 行を持つルートが到達不能なら、カタログを置換せずマーカーも進めない。行を持たない未使用ルートはスキャンを妨げない |
| `StartupSequenceTests` | 起動バナーが存在し、`__version__` が解決でき、シグナルハンドラがスキャン待ちより前に登録される |
| `ShippedWiringTests` | 修正が出荷経路（`main.py` / `ErrorText.swift` / `MenuBarViewModel.swift`）に入っている |

## 2. 手動検証（実機）

### V-1 常駐モデルが launchd 経由で読み込まれる

```sh
launchctl kickstart -k gui/$(id -u)/com.yukiorita.MLXBar.Coordinator
# 起動直後から 3 秒ごとに観測
curl -s --unix-socket "$HOME/Library/Application Support/MLXBar/control/coordinator.sock" \
  http://localhost/api/v1/status | python3 -c 'import json,sys;print(json.load(sys.stdin)["preload"])'
pgrep -P "$(launchctl print gui/$(id -u)/com.yukiorita.MLXBar.Coordinator | awk -F'= ' '/pid = /{print $2}')"
```

期待: バナー行 → `Preload starting: 1 resident, 0 last-used` → 数十秒〜2分で
`Preloaded <name> in Ns`、`preload.state == "loaded"`、子プロセスが 1 つ存在。

### V-2 停止ゲートが解除される（本命の修正）

```sh
# 1) アンロードしてゲートを立てる
curl -s -X DELETE -H "Authorization: Bearer $(cat "$HOME/Library/Application Support/MLXBar/control/api-token")" \
  http://127.0.0.1:11435/api/v1/models/loaded
sqlite3 "$HOME/Library/Application Support/MLXBar/state.sqlite3" \
  "select value from metadata where key='api_autoload_suspended';"   # => 1
# 2) 常駐に設定（GUI のピン、または API）
curl -s -X PUT -H "Content-Type: application/json" \
  -d '{"keepLoaded":true}' \
  "http://127.0.0.1:11435/api/v1/settings/models/pool/profiles/<MODEL_ID>" >/dev/null
sqlite3 "$HOME/Library/Application Support/MLXBar/state.sqlite3" \
  "select value from metadata where key='api_autoload_suspended';"   # => 0
```

期待: 2 の直後に `0`。ログに `Resident auto-load gate cleared (model pinned as resident)`。
その後 V-1 を実行すると常駐モデルが読み込まれる。

### V-3 無効な常駐IDが警告になる

`models.pool.profiles[0].modelId` を存在しない ID に書き換えて再起動する。

期待: `preload.state == "failed"`、`code == "RESIDENT_MODEL_MISSING"`、
ログに `Resident model <id> is no longer in the model list`、
メニューバーに「常駐に設定したモデルがモデル一覧に見つかりません…」（日英）。

### V-4 未マウントのルートでカタログが消えない

```sh
# モデル行を持つルートを一時的に「存在しない」状態にする（設定を編集して再起動）
sqlite3 "$HOME/Library/Application Support/MLXBar/state.sqlite3" "select count(*) from models;"
```

期待: ログに `Model scan skipped: <root> unreachable but still holds catalog entries`、
モデル行数が減らない、`catalog_*_version` マーカーが進まない。

### V-4b 応答しないボリュームで永久に止まらない

許可を外した状態（システム設定 › プライバシーとセキュリティ › リムーバブルボリュームで
MLXBar をオフ）で launchd 経由で起動する。

期待: 起動バナー → `Preload starting` → 20秒以内に
`Reading model config did not finish within 20s` の WARNING と
`Could not preload <id>: ...` / `preload.state == "failed"`、`code == "MODEL_VOLUME_UNREADABLE"`。
メニューバーに許可の案内が出る。**「読み込み中」のまま残らない。**

修正前の実測（3.3.3 以前）: 200秒以上 `running` のまま、子プロセス0、ログ追加0行。

### V-5 互換性（既存動作の維持）

| 手順 | 期待 |
|---|---|
| `DELETE /api/v1/models/loaded` → 再起動 | 常駐モデルは読み込まれない（ゲート維持） |
| 常駐を解除（`keepLoaded:false`）→ 再起動 | 読み込まれない |
| 無関係な設定変更 → 再起動 | ゲートの状態が変わらない |

## 3. ログで確認できること（診断性）

修正後は、常駐モデルが読み込まれない場合に必ず次のいずれかが残る。

| ログ | 意味 |
|---|---|
| `Preload skipped: ... auto-load gate is still armed` | 意図的なアンロード後。モデルを読み込むか常駐設定で解除される |
| `Preload skipped: no resident model is configured` | 常駐設定が無い（正常） |
| `Resident model <id> is no longer in the model list` | 常駐IDが無効。再設定が必要 |
| `Could not preload <id>: <reason>` | 読み込み自体の失敗（メモリ不足など） |
| `Catalog rescan running before the resident preload starts` | スキャン待ち |
| `MLXBar coordinator <ver> starting pid=...` | 起動バナー（バージョン・PID・root・cwd） |
