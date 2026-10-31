# TEST PLAN v3.4.0 — API 診断性（401 / ボリューム停止）

対象: 開発者向けレポート 2件への対応。`RELEASE_NOTES_v3.4.0.md` の主張を、
実行可能な手順と期待結果に対応づける。

## 1. 自動テスト

```sh
cd Coordinator && ./.venv/bin/python -m pytest ../Tests -q
swift test
./Coordinator/.venv/bin/python scripts/check_i18n.py
```

| ID | 期待 |
|---|---|
| T-1 | `Tests/test_v3_4_0_api_diagnostics.py` 16件が全て pass |
| T-2 | 全体で 877 passed / 3 failed（`test_lan_listener_switch_keeps_token_required` / `test_disconnect_stops_the_generation_immediately` / `test_buffered_tool_generation_still_emits_heartbeats`。いずれも環境依存の既知 flaky）/ 1 skipped |
| T-3 | `swift test` 28 tests / 0 failures |
| T-4 | `check_i18n` 454 キー欠落 0 |

### 新規テストの内容

| クラス | 固定する内容 |
|---|---|
| `AuthDiagnosticsTests` | 401 に現在の鍵の指紋・発行時刻・`retry_after_ms` が付く／鍵そのものは返らない／`/v1/auth/status` が無認証で答える／**ローテーションは即時失効**／`/v1/models` は認証必須のまま |
| `HealthDiagnosticsTests` | `/health` が無認証で uptime・volume_state・residency を返す／モデル名やパスを含まない／停止中は `volume_state="stalled"`／listener の許可リストに `/health` と `/v1/auth/status` が入っている |
| `VolumeErrorTests` | 停止した読み取りは `retryable=true` と `retry_after_ms`（クールダウン値）を返す／クールダウン中の即失敗でも待ち時間を返す／パスが無ければ `MODEL_VOLUME_DETACHED`／パスは存在するが応答しなければ `MODEL_VOLUME_UNREADABLE`／未設定時は `retry_after_ms` を含まない（既存payloadの形を保つ） |
| `MetadataCacheTests` | 同じパスのメタデータは1回しか読まない／別パスは別に読む |

## 2. 手動検証（実機・公開API）

前提: `TOKEN=$(cat "$HOME/Library/Application Support/MLXBar/control/api-token")`、
API は `127.0.0.1:11435`。

### V-6 401 の指紋で「鍵が古い」と分かる

```sh
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer wrong-key-000000' \
  http://127.0.0.1:11435/v1/models                       # => 401
curl -s -H 'Authorization: Bearer wrong-key-000000' http://127.0.0.1:11435/v1/models \
  | python3 -c 'import json,sys;e=json.load(sys.stdin)["error"];print(e["code"],e["key_fingerprint"],e["token_issued_at"])'
printf '%s' "$TOKEN" | shasum -a 256 | cut -c1-8          # 上の指紋と一致すること
```

期待: 401 の `key_fingerprint` が**自分が持っている鍵の指紋と一致**すれば、鍵は最新で
問題は別にある。一致しなければ鍵が古い。`grep "$TOKEN"` が応答に出ないこと。

### V-7 認証なしで「期待されている鍵」を確認できる

```sh
curl -s http://127.0.0.1:11435/v1/auth/status
```

期待: 200、`token_required: true`、`key_fingerprint` が V-6 と一致、`issued_at` が
`control/api-token` の mtime と一致、**トークン文字列は含まれない**。

### V-8 `/health` が状態を返す

```sh
curl -s http://127.0.0.1:11435/health
```

期待: `status/version/uptime_seconds/auth_required/model_loaded/
resident_model_count/volume_state/preload_state`。**モデル名・パスは含まれない**。

### V-9 ボリューム停止エラーが再試行可能

ボリュームの許可を外した状態（3.3.3 の V-4b と同じ）で、未ロードのモデルへ
`/v1/chat/completions` を投げる。

期待: 20秒後に 503、本文に `code`（`MODEL_VOLUME_UNREADABLE` または
`MODEL_VOLUME_DETACHED`）、`retryable: true`、`retry_after_ms`（60000 前後）。
`/health` の `volume_state` が `stalled` になる。60秒後に再試行すると再度読み取りを試みる。

## 3. 回帰（壊していないこと）

| 手順 | 期待 |
|---|---|
| 古い鍵で `/v1/models` | 401（無認証化していない） |
| `PUT /api/v1/settings/api-token` でローテーション → 旧鍵で `/v1/models` | **即 401**（猶予なし） |
| `requireToken=false` | `/v1/models` 200、`/v1/auth/status` の `key_fingerprint` は null |
| 通常の生成 | 従来どおり 200 |
| 既存の `/health` 利用者（アプリ） | `status:"ok"` を含むため影響なし |
