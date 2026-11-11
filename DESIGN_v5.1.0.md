# DESIGN v5.1.0 — 単一モデルでの連続バッチ処理（Continuous Batching）と、モデル永続化の暴走修正

> 対象: MLXBar v5.0.2（build 72）からの機能版（build 73）。
> **既定は完全に無効**で、v5.0.2 の経路は変えません。連続バッチ処理を有効にしたモデルだけが
> 新しい経路を通ります。永続化の修正は「API要求でモデルが勝手に常駐し続ける」不具合の修正で、
> 既定値・設定スキーマは変えません。
>
> 関連文書: `TEST_PLAN_v5.1.0.md`、`RELEASE_NOTES_v5.1.0.md`、`CHANGELOG.md` [5.1.0]、
> `scripts/verify-batch-runtime.py`

---

## 1. 目的

要望書（`名称未設定.txt`）の「MLX-LM / MLX-VLM における単一モデルロードでの Continuous Batching 対応」と、
ユーザーから追加で報告された「モデルの永続化がモデルリクエストなどしていると勝手に起こってしまう問題」を扱います。

| # | 要求 | 本設計での対応 |
|---|---|---|
| R1 | 同一モデルを1回だけロードして 2〜8 セッションを同時処理する | F-2 / F-3（mlx-lm の `BatchGenerator` をワーカー内で駆動） |
| R2 | リクエストごとの KV キャッシュ・生成状態を独立管理する | F-2（`BatchGenerator.insert` は per-sequence キャッシュを割り当てる。UID で対応付け） |
| R3 | OpenAI / Anthropic 互換 API の既存仕様を維持する | F-2（イベント語彙は既存のまま。API 層は無変更） |
| R4 | 非対応モデルは従来の単一生成へフォールバックする | F-2（`batch_fallback` → 単一経路で最初から実行） |
| R5 | レプリカ方式とは別機能として扱う | F-3（バッチ可能なスロットは `_maybe_expand_replica` を呼ばない） |
| R6 | 設定は `models.pool.profiles` の `continuousBatching`、既定は無効 | F-1（スキーマ・検証・CLI・GUI） |
| R7 | メモリ逼迫時は新規追加を拒否する | F-3（`_batched_sequence_ok` でヘッドルームを計上） |
| R8 | 切断・キャンセルで当該要求だけ止め、枠とメモリを残さない | F-2 / F-3（`remove(uid)` と `gen_active` の同期解放） |
| R9 | 生成途中の不具合で黙って再生成・再送信しない | F-2（バッチ全体の失敗はエラーとして通知。再試行しない） |
| R10 | **API 要求でモデルが勝手に永続化しない** | F-4（load origin の導入と reaper の修正） |
| R11 | 日英の完全対応 UI | F-5（`LS()` + `en.lproj`、`check_i18n` 0 欠落） |

### 1.1 MLX-VLM について（判断の記録）

要望書 §4 は mlx-vlm の `ResponseGenerator` 再利用を提案しています。実装前に次を確認しました。

- `mlx_vlm.server.generation.ResponseGenerator.__init__(model_path, ...)` は **自分でモデルを
  ロードする**（0.7.12 実測）。MLXBar の `MLXVLMAdapter.load()` と併用すると二重ロードになる。
- `ResponseGenerator` を唯一の所有者にすると、MLXBar の `MLXVLMAdapter` が持つ
  bundled loader（Niwaki）・prompt cache / APC / checkpoint・KV 量子化・
  `_disable_batch_invariant_for_bundled`・`_settle_interrupted_cache` をすべて失う。
  これらは v3.2〜v5.0.2 で個別に不具合を直してきた経路であり、置き換えは
  「既存機能を壊さない」という前提に反します。
- また `ResponseGenerator` は同一のサンプリング設定の要求だけを同じバッチにまとめます
  （`_batch_key`）。MLXBar の API は要求ごとに temperature / top_p を受け取るため、
  そのままでは「同一モデル・複数セッション」の大半が別バッチになります。

したがって **v5.1.0 では mlx-vlm の連続バッチ処理を実装しません**。ワーカーは
`continuousBatching: {capable: false, reason: "engine_not_supported"}` を返し、
GUI は「連続バッチ非対応のため単一生成で動作します」と表示します。将来の実装方針は §7 に記します。

---

## 2. 現状（v5.0.2）の構造と、そこで起きていたこと

### 2.1 生成は「1 Worker = 1 生成」

- `Workers/common/server.py`: `ThreadPoolExecutor(max_workers=1)` で MLX を直列化し、
  1 要求 = 1 `adapter.stream()` イテレータを 1 トークンずつ回す。
- `Coordinator/mlxbar/workers/model_pool.py`: `PoolSlot.gen_lock` / `gen_owner` が
  「1 スロット 1 生成」を前提にしている。2 本目は `_maybe_expand_replica()` で
  **モデルのコピーをロード**する（レプリカ方式）。

### 2.2 永続化の暴走（実測・構造）

`_load_replica()` の「既にレディなスロット」ショートカットは `pin=True` のとき
`_pin_slot()` を呼び、`session_pinned = True`（idle TTL の対象外）と
`_promote_if_room()`（常駐枠の消費）を行います。問題は **`pin` が「明示ロードか」ではなく
「保持したいか」の意味で、呼び出し元が混在していた**ことです。

| 呼び出し元 | v5.0.2 の `pin` | 実態 |
|---|---|---|
| GUI / CLI の手動ロード | `True` | 明示。pin が正しい |
| 起動時 preload | `True` | 明示的設定に基づく。pin が正しい |
| **`_scale_up_pinned_replicas()`（reaper・30秒毎）** | `base.session_pinned` | **手動ロード済みなら True** |
| `load_for_api()`（API 要求時の自動ロード） | `False` | 自動 |

結果として:

1. 一度でも手動ロードしたモデルは、アンロードするまで `session_pinned` のまま。
   `idleTTLSeconds` が効かず、`maxResidentModels` の入れ替えも起きない。
2. reaper は `keep_loaded or session_pinned` のモデルを設定レプリカ数までトップアップするため、
   `profiles[].replicas` が残っていると **API 要求のたびにレプリカが増え続ける**（メモリの暴走）。
3. さらに `_ensure_requested_model()`（API 自動ロード）が `last_loaded_model_id` を
   上書きしていたため、**API 要求で自動ロードされたモデルが次回起動時の復元対象**になっていた。

---

## 3. 機能設計

### F-1 設定（`models.pool.profiles[].continuousBatching`）

```json
{
  "modelId": "MODEL_ID",
  "keepLoaded": true,
  "continuousBatching": {
    "enabled": false,
    "maxSequences": 2,
    "prefillBatchSize": 2,
    "prefillStepSize": 2048,
    "fallback": true
  }
}
```

| キー | 既定 | 範囲 | 意味 |
|---|---|---|---|
| `enabled` | `false` | bool | 有効・無効 |
| `maxSequences` | `2` | 1–8 | 同時に処理するシーケンス数 |
| `prefillBatchSize` | `2` | 1–8 | 同時に前処理するプロンプト数 |
| `prefillStepSize` | `2048` | 256–32768 | 前処理の 1 ステップのトークン数 |
| `fallback` | `true` | bool | 非対応時の従来方式への切り替え |

- 検証は `SettingsStore._validate`（唯一の正本）と `PUT /api/v1/settings/models/pool/profiles/{id}`
  （GUI 向けの詳細メッセージ）の 2 か所。不正な値は書き込み時に拒否し、リクエスト時に
  黙って無視することはしません。
- プロフィール PUT は **フィールド単位でマージ**します（`continuousBatching` の一部だけを送れる）。
  `replicas` / `keepLoaded` / `maxMemoryGB` の既存の部分更新規則と同じです。
- `replicas` は「モデルのロードコピー数」、`generationConcurrency` は「レーン数」の意味のままです。

### F-2 ワーカー: `BatchScheduler`（`Workers/common/batch.py`・新規）

1 つのモデルにつき 1 本のスケジューラスレッドが、MLX の `BatchGenerator` を駆動します。

- **1 GPU スレッド**: MLX はスレッドごとにストリーム状態を持つため、MLX 呼び出しは
  すべてスケジューラスレッド上で行います。asyncio 側とは `queue.Queue` で受け渡します。
- **要求の登録**: `BatchGenerator.insert([tokens], max_tokens=[n], samplers=[...],
  logits_processors=[...], stop_sequences=[...])`。要求ごとのサンプラ・停止条件・
  トークン上限をそのまま渡すので、**セッション間で出力が混ざらない**。
- **進行**: `next()` が返す `PromptProcessingBatch.Response`（前処理の進捗）と
  `GenerationBatch.Response`（token / finish_reason / prompt_cache）を要求ごとのイベントへ変換。
- **終了**: `finish_reason` が付いた要求はバッチから外れ、その KV キャッシュは
  `BatchGenerator` 側で破棄される。`length` の最終トークンは出力に含め、
  `stop` を成立させたトークンは **出力しない**（停止文字列の漏出防止）。
- **キャンセル**: `remove([uid])`。要求単位で KV を回収し、他のシーケンスには影響しない。
- **失敗**: `next()` の例外は **バッチ全体のエラー**として全要求へ配り、再試行しません。
  途中まで出たトークンはそのまま（黙って再生成・再送信しない）。
- **能力判定**: `make_prompt_cache(model)` の全レイヤが `merge` を持つか（mlx-lm の
  `ModelProvider._is_batchable` と同じ条件）を `hasattr` で判定します。バージョン番号では分岐しません。
  ハイブリッド・再帰型キャッシュは `merge` を持たないため、自動的に「非対応」になります。

### F-3 ワーカー: mlx-lm アダプタのバッチ経路と、透明なフォールバック

- `MLXLMAdapter.load()` の最後に `_init_batch_scheduler()`。有効時のみスケジューラを
  用意し（スレッドは初回バッチ要求で起動）、`capabilities.continuousBatching` に
  `capable` / `reason` / 上限を載せます。
- `stream_batched(request_id, params, prompt)` は `stream()` と **同じイベント語彙**
  （`delta` / `token_progress` / `usage` / `metrics` / `completed`）を出します。したがって
  tool 分割・stop フィルタ・heartbeat・メモリ監視・キャンセルは既存コードをそのまま共有します。
- 次の要求はバッチ対象外とし、`batch_fallback` イベント（**最初のトークンより前**）を出してから
  単一経路で実行します。クライアントには何も送られていないので「黙った再生成」ではありません。
  - `seed` 指定（mlx-lm 上流も拒否する。`temperature: 0` の要求は coordinator が `seed: 0` を
    付けるため、決定的な要求もここに含まれます）
  - KV キャッシュ量子化（`kv_bits` 有効）
  - アダプティブメモリ（`adaptiveMemory.enabled`）
  - 画像入力
- **プロンプトキャッシュはバッチ経路でも使います**（v5.1.0）。シーケンスごとに保存済みの
  最長プレフィックスを要求し、終了時にそのシーケンスの KV を保存し直します。再利用は
  「保存側が返した残りトークンがこのプロンプトの厳密な接尾辞である」と証明できたときだけ行い、
  `BatchGenerator` がマージできるキャッシュ型へ載せ替えます。載せ替えに失敗した場合・
  `insert` が拒否した場合は、**冷たいプレフィルへ退避**します（まだ出力はありません）。
  実測では、単一経路のキャッシュ型をそのまま渡すと **出力が空になる**（停止トークンだけが
  返る）ため、この載せ替えは必須です。
- `Workers/common/server.py` の `select_stream()` が経路を選びます。アダプタが
  `stream_batched` / `batch_status` を持たない（=mlx-vlm）場合は従来の `stream()` のままです。

### F-4 Coordinator: バッチレーンとレプリカの排他

- `PoolSlot.gen_active`（同時に処理中の要求 ID 集合）を追加。バッチ可能なスロットでは
  `gen_lock` を取らずに、プール全体の許可（セマフォ）とメモリのヘッドルームだけで並行します。
- `_acquire_batched_lane()` は await のたびに「空きシーケンス数」を再確認します。
  `asyncio.Semaphore.acquire()` は空きがあっても制御を返すため、この再確認がないと
  `maxSequences` を超えて投入され得ます。
- メモリ: 追加シーケンスごとに `perGenerationHeadroomGB`（0 ならモデル上限の 15%、最大 2 GB）を
  計上し、`_batched_sequence_ok()` が予算を超えるなら **バッチに入れず** 通常レーンへ回します
  （=順番待ち。既存のキュー・heartbeat がそのまま働く）。
- レプリカとの排他: バッチ可能で空きがあるスロットでは `_maybe_expand_replica()` を呼びません。
  `maxSequences` を超えた要求は従来どおり（レプリカ拡張 → 順番待ち）です。
- ライフサイクル: `unload()` はスケジューラを停止してから重みを落とします
  （スケジューラがモデルへの参照を残さない）。ロード解除・再起動中の新規要求は
  既存の `ENGINE_BUSY` / `MODEL_NOT_LOADED` 契約のままです。
- 状態: `loadedModels[].continuousBatching`（`enabled` / `capable` / `maxSequences` /
  `activeSequences` / `prefillBatchSize` / `prefillStepSize` / `fallback` / `fallbackReason`）と
  `loadedModels[].batchActiveSequences` を **増分** で追加。旧 GUI は無視できます。
  `activeGenerations` はバッチ中はアクティブなシーケンス数を数えます。

### F-5 GUI（日英完全対応）

- 設定 → モデル → **「連続バッチ処理（実験的）」**: モデルごとの有効化、同時シーケンス数、
  現在の対応状態（対応 / 単一生成で動作 + 理由）。書き込みはモデル単位のプロフィール PUT のみ。
- メニューバーのモデル行: バッチ中（2 本以上）のときだけ「連続バッチ: N/M 並列」、
  有効だが非対応なら理由を表示。アイドル行には出しません（1 秒ごとの表示揺れを避ける）。
- 新規文言は `LS("日本語")` をキーに `en.lproj` へ追加（`scripts/check_i18n.py` で 0 欠落）。
- CLI: `mlxbarctl config set-continuous-batching --model ID --enabled true --max-sequences 4 ...`
  （GUI と同じ範囲検証、同じエンドポイント）。

### F-6 永続化の修正（load origin）

`ModelPoolSupervisor.load(..., origin=...)` を追加し、**明示ロード（`manual` / `preload`）だけが
pin できる**ようにしました。

- `_load_replica()` は `origin` が明示でなければ `pin` を無視します。reaper が
  `pin=<現在の pin 状態>` を渡しても、もう固定にはなりません。
- `_scale_up_pinned_replicas()` は **`keepLoaded` プロフィールだけ**をトップアップ対象にします
  （セッション固定は「1 コピーを保持する」意味であって、背景で増やす許可ではありません）。
- API 自動ロードは `last_loaded_model_id` を書き換えず、`last_api_model_id` に記録します。
  起動時 preload は `last_loaded_model_id` だけを見るため、**API 要求で自動ロードされた
  モデルが次回起動時に復活することはありません**。
- `POST /models/{id}/load` は `pin: false` のとき `last_loaded_model_id` を更新しません
  （明示的に一時ロードしたモデルを「次回復元」として記録しない）。
- 単一アンロードは `last_loaded_model_id` と `last_api_model_id` の両方を忘れます。

---

## 4. 変更ファイル一覧

| 種類 | ファイル | 内容 |
|---|---|---|
| 新規 | `Workers/common/batch.py` | `BatchScheduler` / `BatchRequest` / 能力判定 |
| 変更 | `Workers/common/server.py` | `select_stream()` / `_with_batch_fallback()`。`/generate` の 1 行を差し替え |
| 変更 | `Workers/mlx_lm_worker/adapter.py` | `_init_batch_scheduler` / `batch_status` / `_batch_eligible` / `stream_batched` / `_BatchDetokenizer` / `unload` での停止 |
| 変更 | `Workers/mlx_vlm_worker/adapter.py` | `capabilities.continuousBatching = {capable: false, reason: "engine_not_supported"}` |
| 変更 | `Coordinator/mlxbar/workers/model_pool.py` | `EXPLICIT_LOAD_ORIGINS` / `load(origin=)` / `gen_active` / `_acquire_batched_lane` / `_release_batched_lane` / `_batched_sequence_ok` / `_batch_status` / reaper 修正 / 環境変数の適用と復元 |
| 変更 | `Coordinator/mlxbar/workers/supervisor.py` | `_configure_batch_environment()`、`load(**kwargs)` の互換 |
| 変更 | `Coordinator/mlxbar/settings.py` | `continuousBatching` の検証 |
| 変更 | `Coordinator/mlxbar/api/management.py` | プロフィール PUT のマージ・検証、`origin="manual"`、`last_api_model_id` の消去 |
| 変更 | `Coordinator/mlxbar/api/openai_compat.py` | API 自動ロードは `last_api_model_id` に記録 |
| 変更 | `Coordinator/mlxbar/main.py` | preload の origin |
| 変更 | `Coordinator/mlxbar/cli.py` | `config set-continuous-batching` |
| 変更 | `Sources/MLXBar/Settings/MLXBarSettingsView.swift` | 「連続バッチ処理（実験的）」セクション |
| 変更 | `Sources/MLXBar/MenuBar/MenuBarViewModel.swift` / `MenuBarView.swift` | 状態表示 |
| 変更 | `Sources/MLXBar/Resources/en.lproj/Localizable.strings` | 7 キー追加 |
| 新規 | `Tests/test_v5_1_0_batch.py` / `Tests/test_v5_1_0_persistence.py` | §6 |
| 新規 | `scripts/verify-batch-runtime.py` | 実機 e2e（実コーディネーター + 実ワーカー） |
| 版数 | `Packaging/Info.plist` 他 | 5.1.0 / build 73 |

---

## 5. 安全性・互換性の設計判断

### D-1 既定で無効（v5.0.2 と同一経路）

`continuousBatching` が無いプロフィールは `enabled: false` として扱われます。ワーカーには
バッチ用の環境変数が渡らず、`BatchScheduler` も作られません。`select_stream()` は
`batch_status().capable == false` を見て従来の `stream()` を返すため、**v5.0.2 と
バイト等価の経路**です。

### D-2 非対応モデル・非対応要求は必ず単一生成へ

能力判定は「ロード済みモデルのキャッシュが `merge` を持つか」で行い、判定できない場合は
非対応として扱います。`seed` / KV 量子化 / プロンプトキャッシュ / アダプティブメモリ / 画像は
明示的に拒否します。**「証明できないならバッチしない」** を既定にしています。

### D-3 失敗時の扱い

- バッチ全体の例外 → 全要求に `GENERATION_FAILED`。スケジューラは新しい `BatchGenerator` で
  再構築し、次の要求から続行します（途中の要求を再送信することはありません）。
  例外はワーカーのログに traceback 付きで残します（`Workers/common/server.py` の
  生成失敗ログ）。
- 個別要求の `insert` 失敗 → その要求だけエラー。バッチは継続。
- クライアント切断 → その要求だけ `remove(uid)`。枠（`gen_active` / セマフォ）は
  `generate_for_model` の `finally` で同期解放。
- スケジューラ停止（アンロード・終了）→ 待機中の要求にエラーを配り、スレッドを join。

### D-4 メモリ安全性

- 追加シーケンスごとにヘッドルームを計上し、予算超過ならバッチに入れない。
- 出力キューは 1 要求 4,096 イベントで上限（読まれないクライアントはキャンセル扱い）。
- `BatchGenerator` は要求終了時にその KV を破棄する。アンロード時は `close()` で
  wired limit を元に戻す。
- レプリカを増やさないため、**同一モデル並列時のメモリ増分は KV のみ**（重みは共有）。

### D-5 永続化の修正は既定値を変えない

`idleTTLSeconds`・`maxResidentModels`・`keepLoaded` の意味は変わりません。変わるのは
「手動ロードしたモデルが永久固定になる」という **意図しない** 挙動だけです。
`keepLoaded: true` のプロフィールは従来どおり固定で、reaper も従来どおりレプリカを補充します。

### D-6 既存の回帰テストへの影響（意図した仕様変更 1 件）

`Tests/test_openai_tools.py::test_api_request_restores_requested_model_after_restart` は
「API 要求で自動ロードしたモデルが `last_loaded_model_id` に記録される」ことを固定していました。
これはまさに今回直す挙動（API 要求でモデルが永続化する）であるため、期待値を
`last_api_model_id` に更新し、`last_loaded_model_id` が **変わらない**ことを検証する形にしました。

### D-7 新規エラーコードなし

バッチ経路は既存の `GENERATION_FAILED` / `ENGINE_BUSY` / `MODEL_NOT_LOADED` / `QUEUE_FULL` を
そのまま使います。`schemaVersion=1`、`CATALOG_CLASSIFIER_VERSION` も不変です。

---

## 6. テスト方針

| 種別 | ファイル | 内容 |
|---|---|---|
| 単体 | `Tests/test_v5_1_0_batch.py` | スケジューラ（2 シーケンス同時・要求単位キャンセル・`length`・全体失敗で再試行なし・能力判定・shutdown）、ワーカーの経路選択（非対応/対応/フォールバック/API なし）、アダプタの拒否規則、プールのバッチレーン（レプリカを作らない・既定無効・上限・環境変数・status）、設定検証 |
| 単体 | `Tests/test_v5_1_0_persistence.py` | API 自動ロードが pin しない・常駐枠を消費しない・TTL で解放される、手動ロードは pin する、reaper が `session_pinned` をトップアップしない、`keepLoaded` は補充する、未知の origin は一時扱い |
| 回帰 | `Tests/test_model_pool.py` | 固定 sleep を条件待ちに変更（v5.1.0 で追加した `asyncio.to_thread` 経路により、固定 0.05 秒では中間状態を観測していたため。アサーションは不変） |
| 回帰 | `Tests/test_openai_tools.py` | D-6 のとおり |
| 実機 | `scripts/verify-batch-runtime.py` | 実コーディネーター + 実ワーカー + 実モデルで 2-1〜2-6 |

実測値は `TEST_PLAN_v5.1.0.md` に記録します。

---

## 7. 将来課題

- **mlx-vlm の連続バッチ処理**: `ResponseGenerator` を「モデルの唯一の所有者」にするのではなく、
  `MLXVLMAdapter` の prompt cache / KV 量子化 / bundled loader を保ったまま
  `BatchGenerator` を直接駆動する方式が本命です（mlx-vlm の `stream_generate` は
  `BatchGenerator` を内部で使うため、その前処理（画像・APC・KV 量子化）との分離が前提になります）。
- **バッチとプロンプトキャッシュの併用の実測**: v5.1.0 で経路は実装済み（§F-3）だが、
  長い会話でのヒット率と速度は未計測。
- **KV 量子化との併用**: `BatchGenerator` の `max_kv_size` / 量子化キャッシュの `merge` 対応待ち。
- **バッチ中のトークン速度表示**: 現状はシーケンス単位の平均。バッチ全体のスループット表示は未実装。
- **speculative decoding（draft model）との併用**: mlx-lm 上流が batchable から外すため対象外。
