# MLXBar v1.7.0 モデル間同時生成 設計書

更新日: 2026-08-28
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

v1.6.2 の複数モデル常駐（`ModelPoolSupervisor`）を前提に、**別々の常駐モデルが同時に生成できる**
ようにする。1つの MLX プロセスは単一スレッドなので、**同一モデルへの複数要求は従来どおり到着順に
直列**である。増やすのはモデル**間**の並行度だけである。

この版は同一モデル内の並行生成、cold load の並行化（`loadConcurrency` は 1 のまま）、LM Studio 経路の
並行化（従来どおり `_legacy` へ委譲し直列）を対象にしない。

`generationConcurrency == 1` のとき、v1.6.2 と挙動が等価であること（生成順序・キュー・キャンセル・
`/api/v1/status` の値）を不変条件とする。唯一の差は `queue` イベントの `position` がプール全体通し番号
ではなくモデルレーン単位になる点（単一常駐モデルの通常構成では同一）。`enabled=false` 経路は
v1.6.1 とバイト等価。

## 2. v1.6.2 設計からの変更点

`DESIGN_v1.6.2.md §1` は「MLX はユニファイドメモリを使い、複数モデルの同時計算ピークは測定なしに
安全と証明できないため、生成は従来どおり全体で1件に直列化する」と述べていた。v1.7.0 はこの非目的を
更新し、**プロセス分離済みで個別 admission 済みの常駐モデルに限り**、メモリ・ヘッドルームガードと
macOS メモリ圧の監視の下で並行生成を許可する。

**合算 allocation ピークの実機計測は本リリースでも未実施である**（`TEST_PLAN_v1.7.0.md §2`）。
既定値 `generationConcurrency = 2` はオーナーの明示指示による。`common rules` / `mlx-bar.md` の
「実測がないなら既定値を動かさない」方針からは外れる判断であり、その旨を保守メモに記録している。
安全側の設計（下記メモリガード）で、直列なら成功する要求を落とさず、逼迫時は自動的に直列へ降格する。

## 3. アーキテクチャ

```text
OpenAI API / GUI / CLI
          |
          v
 ModelPoolSupervisor
   ├─ per-model lane   : PoolSlot.gen_lock（同一モデルを直列化）
   │                     PoolSlot.gen_owner / gen_recoveries（孤児レーン回復、per model）
   │                     PoolSlot.gen_queued（モデル別キュー）
   ├─ pool-wide permit : asyncio.Semaphore(generationConcurrency)
   │                     _gen_active_lanes（保持中の permit 数）
   └─ memory guard     : _admit_concurrent() + macOS pressure（2レーン目以降のみ）
          |
          +-- model A -> WorkerSupervisor -> process/socket -> mlx-lm / mlx-vlm
          +-- model B -> WorkerSupervisor -> process/socket -> mlx-lm / mlx-vlm
          `-- LM Studio -> legacy provider path（従来どおり直列）
```

生成1件は次を順に取得する。

1. `PoolSlot.gen_lock`（そのモデルのレーン。同一モデルの次要求はここで待つ）。
2. プール全体のセマフォ permit を1つ。
3. permit 取得後に `_concurrent_start_ok()` を再確認し、通らなければ permit を返して待つ。

`finally` で必ず `_release_lane()` を実行し、`gen_lock` 解放・permit 返却・`_gen_active_lanes` 減算を
行う。正常終了・client 切断・cancel・例外・キュー中断のどの経路でも同じ。

## 4. メモリ・ヘッドルームガード（`_admit_concurrent` / `_concurrent_start_ok`）

- **1件目の生成は常に許可**する（v1.6.x と同じ）。ガードは2レーン目以降にのみ適用する。
- 2レーン目以降は次をすべて満たすときだけ開始する。
  - `generationConcurrency > 1`。
  - `_resident_charge()` ＋ Σ(実行中レーンの生成ヘッドルーム) ＋ 新規1件分 ≤ `_global_budget()`。
  - macOS の `kern.memorystatus_vm_pressure_level` が warning 未満（< 2）。
- 生成ヘッドルーム見積 = `models.pool.perGenerationHeadroomGB`（既定 0 = 算出）。
  算出値は `min(perModelLimit × 0.15, 2 GiB)`。範囲は 0.25–32 GB。
- ガードを通らない要求は**失敗させず該当モデルのキューへ回す**。permit が空いても
  `_acquire_lane` 内で再チェックし、通るまで permit を返して 50 ms 間隔で待つ（キュー timeout で上限）。

## 5. キュー / キャンセル契約（v1.6.2 からの再設計）

- キューはモデル別（`PoolSlot.gen_queued: dict[request_id, QueuedRequest]`）。position・待ち時間・
  cancel をレーン単位で計算する。`queued_requests` プロパティは全レーン＋各 Worker を合算した view。
- `QUEUE_FULL` は全レーン合計の待ち件数が `generation.maxQueuedRequests`（既定16）以上のとき。
- `cancel(request_id)`：全レーンの `gen_queued` を走査。待機中ならイベントを立てて即 `cancelled`。
  実行中なら該当 Worker の `cancel` を呼ぶ。`cancel_all` も同様。
- **孤児レーン回復**：`_recover_lane()` を `WorkerSupervisor._recover_orphaned_generation_slot()` と
  同じ論理積（レーン lock 保持中 / owner が active にも queued にも不在 / unowned active request 不在）で
  per model に実装。`new_request` / `queue_wait` / `capacity_check` の各所で評価する。
  v1.6.2 のプール即席キューにはこの機構が無く、切断された handshake がレーンを恒久的に塞ぎ得た。

## 6. 設定契約（`models.pool` への追加）

| 項目 | 既定 | 範囲 | 反映 |
|---|---:|---:|---|
| `generationConcurrency` | 2 | 1–8 | service 生存期に latch。次回起動で反映 |
| `perGenerationHeadroomGB` | 0 | 0 または 0.25–32 | 即時（次の admission 判定から） |

`enabled` と同じく `generationConcurrency` は起動時に読み取って固定する（実行中にセマフォ容量を
入れ替えると片側のレーンが宙に浮くため）。schemaVersion は 1 のまま、既存 config は deep merge で追従。

## 7. 観測

`GET /api/v1/status` に追加：`generationConcurrency`、`activeGenerations`（実行中レーン数）、
`modelPool.generationConcurrency` / `modelPool.activeGenerations`、`loadedModels[].laneQueueDepth`、
`loadedModels[].laneRecoveries`。`generationLockRecoveries` は全レーンの `gen_recoveries` 合計。
`generationLockState` は「どれか active なら active」。GUI（設定画面）に「同時生成 N / 上限 M」を表示。

## 8. 後方互換性

- `generate` / `generate_for_model` / `cancel` / `cancel_all` / `raise_if_queue_full` /
  `wait_until_idle` / `status` のシグネチャと戻り値の形は不変。
- `generation_lock` プロパティは残す（`.locked()` だけを持つ read-only view）。外部からの
  ロック取得は元々していない。
- `enabled=false` は完全に `self._legacy` へ委譲（v1.6.1 経路）。
- OpenAI 互換の形（`/v1/models`、Chat Completions、128 tools 上限、bearer 認証、SSE keep-alive
  コメント）は不変。ルーティングは従来どおり解決した model ID の Worker へ。

## 9. B. 複数モデル事前ロードの UX（同梱）

- `POST /api/v1/models/{id}/unload`（`?force=`）を追加。1モデルだけ解放し、他は常駐のまま。
  そのモデルの lease があるときだけ `ENGINE_BUSY`。既存の `DELETE /models/loaded`（全解放）は据え置き。
- `ModelPoolSupervisor.unload_model(model_id, *, force=False)`。
- `mlxbarctl`：`model unload <id>` / `model resident` / `model pin <id>` / `model unpin <id>` を追加。
  `model unload`（引数なし）は従来どおり全解放。
- GUI：設定画面に `generationConcurrency` の Stepper と「常駐させるモデル」（`models.pool.profiles`）の
  トグル一覧。起動時プリロード（`main.py:_preload_last_model`）は v1.6.2 から挙動を変えず、
  常駐に成功した集合を1行ログに出すだけ。

## 10. 受け入れ基準

1. モデル間並行 / 同一モデル直列 / セマフォ上限遵守 / メモリガードのキュー降格 / per-lane cancel /
   孤児レーン回復 / `generationConcurrency=1` の回帰、の自動テスト（`Tests/test_model_pool.py`）。
2. OpenAI 互換：2 常駐モデルへの同時ストリームが API 層で直列化されないこと、128/129 tools 境界、
   認証（`Tests/test_openai_tools.py`）。
3. v1.6.2 までの全テストを含む回帰（`Coordinator/.venv/bin/python -m pytest Tests -q`）。
4. Swift Debug/Release build。
5. **実機（`TEST_PLAN_v1.7.0.md`）**：2 モデル常駐での同時ストリーム時の RSS / `vm_stat` /
   pressure level、OpenClaw を異なるモデルで2エージェント同時実行して相互ブロックが無いこと。
   合算ピーク検証が未実施の間は「未実施（要実機）」と明記する。
