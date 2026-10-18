# DESIGN v2.8.1 — 生成レーン整合性 / イベントループ詰まり撤去 / GUI・CLI パリティ精査

- 版数: app 2.8.1 / build 54。Coordinator `__version__` 2.8.1。`schemaVersion` は 1 のまま（変更なし）。
- 方針: 既定値不変・API 形状不変・破壊的変更なし。v2.8.0 のパッチリリースで、v2.8.0 対象外の精査指摘（v2.7.5 で「production で修正予定」と記載しながら v2.8.0 に含まれていなかった生成レーン競合を含む）を解消する。
- v2.8.0 の非常駐モデル・待機延長・協調的取消は維持する。

## 1. 背景（監査結果）

- 依頼: memory/clash safety・stability・GUI/CLI completeness・config consistency・backward compat・latest runtime support の精密監査 → 修正プラン → 実装 → 設計評価とデバッグ → release → Web 更新。安全設計・起動クラッシュ安全性・日英完全対応 GUI を守る。
- 監査で見つかった主要指摘:
  1. **(H-1) プール生成レーンの取り残し**: v2.8.0 ストレステストが `_gen_active_lanes` が
     1 で止まり `gen_permit` がすべて False・`_gen_slots` が 1 本不足のまま復帰しない状態を
     散発的に再現。原因は「親に放棄された in-flight の `_acquire_lane` タスクが orphan 回復
     で解放されたレーンに対して commit してしまう」zombie wake-up 競合（v2.7.5 で
     「production で修正予定」と記載されながら v2.8.0 に含まれていなかった項目）。
  2. **(H-2) イベントループ詰まり**: `status()` ポーラー（1秒毎）が `getaddrinfo` と `collect_hardware`（`subprocess.run`×3 + `shutil.disk_usage`、各 5 秒タイムアウト）を直接叩く。逼迫ホストで全リスナーが秒単位で固まる。
  3. **(H-3) GUI/CLI パリティ**: `mlxbarctl config set-kb` の chunk-overlap は GUI と違い交差検証をしない。`mlxbarctl config set-lan false` は LAN 無効化でも `requireToken` を無条件 true に上書きし、localhost 専用で意図的にトークンを切ったユーザーを黙って戻す。`apply-preset custom` が GUI と違い拒否された。WAIT_JOB も無期限 hang しうる。
  4. **(H-4) 設定一貫性**: 設定エクスポートの `api.host` / `security.allowLan` は leaf 単位適用では `_validate`（必ずペアで変えること）に leaf 単独で 1 つずつ失敗し、LAN 構成が転送できない（v2.6.0 での退行）。

## 2. 修正内容

### 2.1 生成レーン整合性（model_pool.py、H-1）

- **zombie wake-up 競合**: `asyncio` プリミティブの中で suspend されたタスクの cancel は、同一瞬間に orphan 回復や finalizer が同じプリミティブを release すると停止しないことがある。復活した `_acquire_lane` は「誰も所有しない要求」のために owner/permit/`_gen_active_lanes` を commit し、レーンと semaphore 1本を永久消費する（実測した take から `_gen_active_lanes==1`・`gen_permit` 全部 False・`_gen_slots` 1 本不足）。
- **abandon marker**: `_abandoned_lanes`（親 finalizer が `slot.gen_queued.pop`・`_release_lane`・cancel の**前**に投入）と `_lane_tasks`（取得タスクの登録）。`_acquire_lane` は全 await の resume 後に marker を再確認し、投入されていれば raw hold 状態のみを rollback（`raw_permit` が真の時だけ permit を返却、owner 一致時だけ lane lock 返却）して commit なしで戻る。marker は取得タスク自身の `finally` が消費する。
- **二段オーファン判定（グレース）**: `_recover_lane` は同じ orphan 状態を 2 回連続観測（0.5 秒幅）しなければ回復しない。レーン commit と worker 登録の間の live 要求は 1 event-loop step 未満しか「消失」に見えないため、status poll が生きている要求を殺さない。生存確認（owner が active/queued に見える）で pending 判定をクリア。
- **レーン喪失の表面化**: `await acquire_task` 後に `gen_owner != request_id` になったら黙って非直列化で生成せず `ENGINE_BUSY` (503, retryable) を発生。生成要求は worker マップ登録を cancel 待ちの yield 前（レーン commit 直後）に行い、この窓からは消失状態に見えなくする。
- 既定値・設定キー・API 形状は不変。v2.8.0 の協調的取消・孤立生成上限は影響なし。

### 2.2 イベントループ詰まり（management.py、H-2）

- `_cached_lan_addresses` を async 化し `getaddrinfo`（`lan_ipv4_addresses`, 込み）を executor へ。1 秒ポーラーが slow resolver に詰まらなくなる。キャッシュ挙動・応答形状は不変。
- `collect_hardware`（3 件の連続 `subprocess.run` + `shutil.disk_usage`）を `_hardware_async` に集約し `asyncio.to_thread` で実行。`/system/hardware`・`/presets/status`・`/presets/apply`・export・setup-status の 5 経路すべて。`subprocess.run` 内部は不変。
- `unload` / `runtime activate` / `runtime rollback` の `workers.unload()` 失敗（チェックと unload の間に取られた lease など）を `HTTPException(status, detail)` で返す。MLXBarError 直投げによる 500 化を撤去。

### 2.3 GUI/CLI パリティ（cli.py、H-3）

- エラーレスポンスが JSON でない場合（proxy・crashed handler）に生テキストをエラーとして見せる。parse エラーで本当の失敗を隠さない。
- `wait_job` に 30 分上限（`JOB_WAIT_TIMEOUT`、GUI の cap と同一）。wedged job でも CLI が hang しない。
- `config apply-preset custom`: GUI と同じ契約（値を標準プリセットから外した記録）として settings への plain PUT を追加。サーバー側 `/presets/apply` は従来どおり `custom` を拒否する。
- `config set-kb --chunk-overlap`: GUI（Knowledge base panel）と同じ交差検証（`0 <= overlap <= chunkSize/2`、chunkSize は現在設定値 or 同時指定値）を CLI にも入れる。
- `config set-lan`: `requireToken` の強制は **LAN を有効にするときだけ** に変更。無効化の Hollandhost でトークン設定を黙って書き換えない（settings.py の `_validate` と整合、既定値不変）。

### 2.4 設定一貫性（settings.py / settings_export.py、H-4）

- `api.enabled` に bool 検証。generation の int/数値レンジ検証は `bool`（`int` の subclass）を明示拒否 — `config set` / import で `true` を 1 として通さない。
- 設定 import で `api.host` + `security.allowLan` を**ひとつの候補クラス**として検証・適用する。LAN 構成のエクスポートが LAN のまま転送される。片方だけの export は従来どおり leaf 単位で skipped 理由表示。
- **runtime auto-check ループ（main.py）**: `runtimes.mlx-lm.autoCheck` / `checkIntervalHours` がこれまで検証されるだけで使われていなかった（v2.7.1 に導入したキー、GUI は autoCheck 表示を既に参照している）。 consuming する background ループを追加。手動チェックと同じ `runtime_check:{engine}` metadata を書くため既存 UI（lastCheck 表示・`runtime update` フロー）は無変更。**install はしない**（明示操作のみ）。既定は不変（autoCheck false / 168h）。

### 2.5 worker（Workers）

- `RUST_NUM_THREADS` / `TOKENIZERS_PARALLELISM` の既定抑制を `common/server.py` に移設し、**mlx-vlm worker も同じ上限を継承**（v2.8.0 は mlx-lm の adapter だけにあり、vlm の最初の tokenisation でコア数分の rayon thread を起動したままだった）。`setdefault` 維持、operator 環境変数は無償。
- GUI（Swift）: `ErrorText` に `INVALID_IDLE_EXTENSION`・`MODEL_NOT_IDLE_MANAGED`（v2.8.0 で GUI にも浮上するため追加）と `JOB_WAIT_TIMEOUT` の日英文言追加。新規 View 追加なし、`Fixed-Size` 不変、en.lproj の accessibility hint キーを `IdleExtendField` の参照キー（句点なし）に一致させた（v2.8.1 未修正だと英語 UI だけ裸キーが出た可能性）。

### 2.6 対応済み・据え置きの確認（変更なし）

- 起動時 crash-safety（v2.7.5 の sqlite open retry）、メモリ上限（chatOutput/StreamBuffer/jobs/runtime_history）、`CancellationRegistry`、prompt-cache 機構、`.strings` 重複ゼロ、ad-hoc 署名、autoCheck 既定 off、cli/proxy 経路の default 不変。

## 3. 下位互換性

- API レスポンス・ステータスコード・SSE フォーマット・設定スキーマ・既定値は不変。
- 新規 error code は追加のみ（`JOB_WAIT_TIMEOUT`・`ENGINE_BUSY` の 503 retryable 化は既存 code の使用経路追加）。
- `set-lan false` の挙動変化は「勝手に true だった」をやめる方向のみで、`true` を明示したい場合は今までどおり GUI/`config set api` で可能。

## 4. 検証

`TEST_PLAN_v2.8.1.md` を参照。自動回帰は **654 passed**（fluke 含まず、新規
`Tests/test_v2_8_1_lane_integrity.py` 3件）。実機スモークと dmg 公開検証も同席以下に記録済み。
