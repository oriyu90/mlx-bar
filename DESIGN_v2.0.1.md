# MLXBar v2.0.1 設計書（同一モデルの追加レプリカが常駐モデル上限で拒否される不具合の修正）

更新日: 2026-09-08
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:** 複数モデル常駐プールで、`maxResidentModels` の入場制御が同一モデルの追加レプリカも
「常駐モデル」として数えてしまい、`maxResidentModels = 1` のときに `replicas = 2` を設定しても
2体目が必ず拒否される不具合を修正する。あわせて、ベストエフォートで見送られたレプリカと、
保存済みだが未反映の `generationConcurrency` を、ステータス／CLI／GUIから確認できるようにする。

**非目的:** OpenAI/Anthropic互換API・管理APIの契約は変更しない。設定スキーマ
（`SettingsStore.DEFAULTS`）は変更しない。メモリ安全性の判定（モデル単位のMLXメモリ上限、
全体メモリ予算、OSメモリ圧、`maxReplicasPerModel`）は各レプリカに対して従来どおり適用し、
一切弱めない。新しいGUIミューテーション（＝新しい名前付きCLIコマンドを要する操作）は追加しない。

調査の一次情報は `INVESTIGATION_v2.0.0_2026-09-08.md`。

## 2. 不具合の実体

### 再現条件

- `models.pool.enabled = true`
- `models.pool.maxResidentModels = 1`
- 固定プロファイルに `replicas = 2`、`maxReplicasPerModel >= 2`
- 2レプリカ分のメモリ予算・OSの空きメモリともに十分

期待は「異なるモデルは1種類まで、同じモデルのWorkerは2体まで常駐」。実際は1体目だけロードされ、
ログに `Could not load replica 1 of <id>; keeping 1 replica(s)` が残るだけで、原因（モデル数上限）
がメモリ縮退と区別できなかった。

### 原因

`Coordinator/mlxbar/workers/model_pool.py` の `_admit()` が、`maxResidentModels` の判定に
`len(self._slots)` を使っていた。`_slots` はモデル数ではなく、同一モデルのレプリカも含む
Worker スロット数である。したがって replica 0 が既に居る状態で replica 1 を入れようとすると、
メモリ予算を満たしていても `len(self._slots) - len(evict) >= maximum` が成立し、
`MEMORY_BUDGET_EXCEEDED` で拒否されていた。

これは次の公開契約と矛盾する。

- 設定UIは `maxResidentModels` を「最大常駐モデル数」と表現する（`MLXBarSettingsView.swift`）。
- ステータスの `residentModelCount` はユニークな model ID 数を返す（`model_pool.py` の `status()`）。
- レプリカ機能（v1.8.0）は「同一モデルの独立Worker」を意図している。

同じ数え方の誤りが `_reap_once()` の live-reduction ブロック（`maxResidentModels` の実行時
引き下げを反映する箇所）にもあった。こちらは候補が固定/keep-loadedスロットを除外するため
固定レプリカが実際に落ちることはなかったが、意味論としては同様に誤っていた。

## 3. 修正内容

### 3.1 `_admit()` — 入場制御をユニークモデルID数で行う

`_admit(model)` 内にローカル関数 `_distinct_models(pending_evict)` を追加し、
「退避予定を除いた常駐スロットのユニークな model ID 数」＋「今回入れる model ID（既に常駐して
いれば集合演算で吸収される）」を返す。退避ループと最終判定を
`len(self._slots) - len(evict) >= maximum` から `_distinct_models(evict) > maximum` へ置換した。

- 同一モデルの追加レプリカを入れる場合、その model ID は既に集合に含まれるため
  `_distinct_models([])` は現在のユニークモデル数（不変条件上 `<= maximum`）に等しく、
  モデル数を理由に退避・拒否されない。
- 新しい model ID を入れる場合は従来どおり `ユニークモデル数 + 1 > maximum` で
  最も古い非固定・非keep-loaded・リースなしのスロットから退避する。`>=` から `>` への
  変化は「今回の1件」を右辺ではなく左辺（集合）へ移しただけで、境界は等価。
- メモリ関連の判定（`estimate > per_model` / `budget` / OSメモリ圧 / 空きメモリ余白 /
  ロード後実測値の各チェック）は一切変更していない。

スロットの同一性判定は `id(slot)` の集合で行い、`PoolSlot`（dataclass）のフィールド単位
`__eq__` を退避リストの `in` 判定で走らせないようにした。

### 3.2 `_reap_once()` — 実行時引き下げも同じ数え方に揃える

live-reduction ブロックの `remaining_count = len(self._slots) - len(victims)` /
`remaining_count <= maximum` を、`_distinct_after(victims)`（犠牲を除いたユニークモデルID数）
`<= maximum` へ置換。余剰レプリカの縮退は従来どおり直前の専用ブロック
（`_desired_replicas` に基づく replica 1..N のトリム）が担当し、`maxResidentModels` の
ブロックはモデルの種類数だけを見る。

### 3.3 診断の追加（読み取り専用）

- `ModelPoolSupervisor.__init__` に `self._replica_admission_failures: dict[str, dict]` を追加。
  `load()` と `_scale_up_pinned_replicas()` のベストエフォート失敗時に
  `{modelId: {desired, ready, code, message, at}}` を記録し、そのモデルが満数に達した時／
  プールから消えた時（`_evict_slot`）にクリアする。トークン・パス・リクエスト本文は記録しない。
- `status()` を `_model_pool_status()` に切り出し、`modelPool` へ
  `configuredGenerationConcurrency`・`effectiveGenerationConcurrency`・`replicaShortfalls` を追加。
  `restartRequired` を「プール有効/無効の差」だけでなく「保存済み `generationConcurrency` が
  実効値（コーディネータ起動時にラッチ）と異なる」場合にも true にする。
- `loadedModels[]` の各行へ `readyReplicaCount`・`desiredReplicaCount`・`replicaShortfall` を追加。
- `mlxbarctl models resident`（`cli.py`）が上記フィールドをそのまま出力する。名前付きの
  新規CLIコマンドは追加していない（GUIの新規ミューテーションが無いため。v1.8.1のパリティ
  契約に沿う）。
- GUI（`MLXBarSettingsView.swift` の「複数モデル常駐」セクション）に、
  `modelPoolRestartRequired` が true のときの注意書きと、`replicaShortfallSummary` がある
  ときの「レプリカ不足」行を追加。`MenuBarViewModel` が `modelPool` から両値を読む。
  日本語はソース文字列、英語は `en.lproj/Localizable.strings` に2キー追加。

## 4. 不変条件（守ったこと）

- `replicas == 1`（全モデルの既定）のプールは v2.0.0 とバイト等価。`_distinct_models` は
  スロット数とモデル数が一致するこのケースで従来と同じ値を返す。
- `maxResidentModels` を超える異なるモデルの追加は従来どおり拒否／LRU退避される
  （`test_model_pool.py` の既存3件が緑）。
- 固定モデル・リース中モデルは退避対象にしない。メモリ予算・OSメモリ圧による安全な縮退の
  経路は無変更。
- 追加した `modelPool` / `loadedModels[]` フィールドはすべて増分。既存キーの削除・改名・
  型変更はない。Swift側は `as? NSNumber ?? 既定` で読むため、旧コーディネータ（フィールド
  なし）でも安全にフォールバックする。
- Anthropic互換API・OpenAI互換API・管理APIのハンドラは1行も変更していない。

## 5. 検証

`TEST_PLAN_v2.0.1.md` を参照。Python回帰 **410件**（v2.0.0の406 + 新規4）。
`swift build --disable-sandbox` 成功。実機で `maxResidentModels = 1` ＋ 同一モデル
`replicas = 2` の2体常駐と、その状態での同時2要求が別レプリカへ振られることを確認する。
