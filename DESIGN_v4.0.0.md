# DESIGN v4.0.0 — ロード／常駐設定の競合と「誤認識」の除去

## 1. 入力

ユーザー要求: 7観点（メモリ安全性・クラッシュ安全性・安定性・GUI の完全性・設定の整合性・
互換性・表示の正確性）で精査し、**特にモデルロードの変なタイミングや常駐設定の変更後でも、
設定が壊れず、アプリが誤認識しない**ようにする。安定版 v4.0.0 としてリリース。

精査（TO-PLAN、`tasks/TASK-007/PLAN/FINDINGS.md`）で 16 件（HIGH 7 / MEDIUM 6 / LOW 3）。
G1 でスコープを確定（`tasks/TASK-007/DECISIONS.md`）。

## 2. 設計方針

**「状態を1つの真実から導出する」**。今回の指摘の多くは、同じ事実を複数の場所が
別々に記憶していたことに由来する。

| 事実 | 以前 | 以後 |
|---|---|---|
| 常駐モデルが今ロードされているか | `preload_state`（試行の記録）と `loadedModel` が別々 | `state.preload_recovered()` が「記録 × 現在のロード状態 × 常駐設定」から導出し、`/status` と `/health` が同じ関数を使う |
| 設定ファイルが書き込み可能か | 読み込み時のフラグと書き込み経路が別 | `_raise_if_settings_read_only()` を5つの書き込みエンドポイントで共有 |
| エラーの表示文 | 各所が `str(exc)` / `exc.message` を混在使用 | `describe_error()` に集約 |
| シャットダウン中か | 概念が無い | `ModelPoolSupervisor._closing`（teardown の最初に立てる） |

## 3. 主要な設計判断

### D-1 失敗記録は「履歴」、表示は「現在」（FIND-01/03/04）

`preload_state` は試行の記録なので消さない。表示時に
「失敗したモデル == 常駐に設定されている == 現在ロードされている」の3条件が揃ったときだけ
`loaded` に読み替える。**別モデルがロードされていても復旧とはみなさない**（これが
「ボリューム障害」と「別モデルのロード」を混同しないための境界）。

### D-2 一時的な失敗は限定的に再試行（FIND-01）

`retryable=True` の失敗だけを 30 秒間隔・最大 3 回。sleep 中は `model_autoload_lock` を
保持しない（API のオートロードを止めない）。unpin・全アンロード（ゲート）・
シャットダウンで即停止し、再試行は `work()` 内で `sleep` するため
`preload_task.cancel()` が構造的に効く。

### D-3 singleflight のキャンセル分離（FIND-05/06）

leader の `CancelledError` は waiter に渡さない。`LOAD_CANCELLED` に変換し、waiter は
**最大2回**まで自分が leader として再入できる。`load_future` は失敗経路の
**await より前**に必ず解決する（teardown が例外を投げても waiter がハングしない）。

### D-4 設定書き込みの直列化とキー単位ロールバック（FIND-07/08）

`SettingsStore.write_lock`（asyncio.Lock）で PUT を直列化し、待ちは 5 秒で打ち切って
409 `SETTINGS_CONFLICT`。リスナ切替に失敗したときの巻き戻しは
`_touched_old_values()` が返す**その要求が触ったキーだけ**。
前方互換ファイル（新しい版が書いたもの）へは 5 つの書き込み経路すべてで
409 `SETTINGS_READ_ONLY` を返し、1 バイトも書き換えない。

> 判断: 拒否は `SettingsStore.update()` ではなく **HTTP 層**に置いた。`update()` 側で
> 拒否すると、既存の AC-COMPAT-1（ダウングレード保存は v1 形状＋未知キー保全で成功する）
> が壊れるため。CLI/内部経路の従来挙動を維持しつつ、API からは書き込み不可を明示する。

### D-5 ロード中のアンロードで固まらない（FIND-10）

アンロード系は `_load_lock` を 5 秒で打ち切り、取れなければ 409 `ENGINE_BUSY`。
**ロード経路は変えない**（長いコールドロードを壊さないため）。
「未完了なのに成功を返す」経路は作らない（タイムアウトは常にエラー、ゲートも立てない）。

### D-6 終了中は新しい作業を始めない（REVIEW R-01）

`_closing` を `shutdown()` の最初（await より前）に立て、`load()`・再入・slot 生成の
3箇所で拒否する。`shutdown()` が slot をスナップショットした後に worker が生まれると
孤児になるため。teardown が失敗しても slot は `finally` で必ず破棄し（R-03）、
呼び出し側には**元のロード失敗**を返す。

### D-7 admission の鮮度（FIND-09/14）

`_admit()` の victim 候補抽出の直前に `keep_loaded` を設定から読み直す。
`status()` / reaper の更新（最大30秒遅れ）に依存しない。
`resident` / `reservation_bytes` の動的再計算は設計に踏み込むため次期（既知の制限）。

## 4. 見送り（既知の制限）

- `status()` の観測副作用（GET が内部状態を修復する）: reaper へ移すと「1ポーリングで表示が直る」挙動を失う
- 常駐数上限を増やしたときの既存非駐在モデルの自動昇格
- ロード中の unpin が admission を再評価しない（R-04）
- `retry_after_ms` を GUI がまだ使っていない（R-06）
- ロード中の一瞬、`/status` と `/health` が evicting 状態で食い違う（R-05）

## 5. 変更点

| ファイル | 変更 |
|---|---|
| `Coordinator/mlxbar/errors.py` | `describe_error()`、`retry_after_ms` |
| `Coordinator/mlxbar/main.py` | preload の復旧導出・限定再試行・バナー・`describe_error` |
| `Coordinator/mlxbar/state.py` | `preload_recovered()` |
| `Coordinator/mlxbar/api/management.py` | 設定ロック／キー単位ロールバック／読み取り専用ガード／ゲート順序／preload 表示 |
| `Coordinator/mlxbar/api/openai_compat.py` | `/health` も同じ復旧導出 |
| `Coordinator/mlxbar/settings.py` | `write_lock` / `revision` |
| `Coordinator/mlxbar/workers/model_pool.py` | `_closing` / future 先行解決 / `LOAD_CANCELLED` 再入 / `_unload_lock` / victim 再読込 |
| `Sources/MLXBar/**` | `skipped` 3分類、新規コード7件の対訳 |
| `Tests/**` | 新規 47 件＋Swift 1件、既知 flaky 3件の決定化（WP-10） |
