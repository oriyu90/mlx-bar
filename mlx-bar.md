# mlx-bar 保守メモ

> 公開物（README・紹介サイト等）には出さない、次回以降の開発向けメモ。
> [common-rules-document](https://github.com/oriyu90/common-rules-document/blob/main/common%20rules.md) ルール6に基づき作成。

## v1.6.2の複数モデルpoolで守ること

- poolは従来の`WorkerSupervisor`を差し替えない。`ModelPoolSupervisor`がmodel単位のSupervisorを合成し、`enabled=false`ではv1.6.1のlegacy経路へ委譲する。互換性調査はまずこの無効経路と`loadedModel`応答を確認する。
- 各native MLX Workerはロード前に`MLXBAR_MLX_MEMORY_LIMIT_BYTES`を`mlx.core.set_memory_limit`へ適用し、load応答の`capabilities.memoryLimits.set_memory_limit`で同じbyte値を返す。runtime probeでこのAPIを確認する契約を削らない。上流APIが改名/削除された場合はpoolが安全に拒否してからadapterを更新する。
- 全体予算は実測RSSだけではなく予約chargeの合計でadmissionする。ロード中の瞬間ピークを見た後では遅いため、事前予約、global load lock、allocator上限、ロード後実測の4層のどれも外さない。
- 生成はモデルごとではなくpool全体のlockで1件。同時生成を有効化するには複数モデルのKV cacheとMetal allocation peakの実機計測、queue/cancel契約の再設計が先に必要。
- TTL/LRUは`leases == 0`でのみ解放する。leaseはstreamを返す前に取得し、正常終了・client切断・cancel・例外を同じ`finally`で解放する。
- `models.pool.enabled`はサービス生存期にlatchし、変更は次回起動時に反映。その他の上限縮小はidle unpinned LRUから適用し、active/pinnedを強制中断しない。
- LM Studioは返された`instance_id`でunloadするが、外部プロセスのメモリをnative pool合計に含めない。byte予約を強制できないのに「予算内」と表示しないことが優先。
- runtime更新は対象engineの全常駐モデルとpinをsnapshotする。1モデルだけを`loaded`から戻すv1.6.1の前提へ戻さない。

## v1.7.0のモデル間同時生成で守ること

- **同時生成はモデル間だけ。** `PoolSlot.gen_lock`が同一モデルを直列化し、プール全体の`asyncio.Semaphore(generationConcurrency)`がモデル間の並行数を束ねる。同一Workerプロセスは単一スレッドなので、同一モデル内の並行はやらない。`generationConcurrency=1`でv1.6.2と等価（生成順序・キュー・キャンセル・`/api/v1/status`）であることが不変条件。唯一の差は`queue`イベントの`position`がプール通し番号→モデルレーン単位（単一常駐モデルなら同一）。`enabled=false`はv1.6.1へ完全委譲でバイト等価。回帰は`test_concurrency_one_serialises_across_models`と既存プールテストで担保。
- **1件目の生成は常に許可。** メモリ・ヘッドルームガード（`_admit_concurrent` / `_concurrent_start_ok`）は2レーン目以降にのみ効く。v1.6.x はメモリ圧を生成に対して見ていなかったので、`_gen_active_lanes == 0`のときは無条件で通す。ここを「常に判定」に変えると単一生成の挙動が退行する。
- **ガードは失敗ではなくキュー降格。** 直列なら成功する要求を絶対に落とさない。permitが空いても`_acquire_lane`内で`_concurrent_start_ok`を再チェックし、通らなければpermitを返して50ms間隔で待つ（キューtimeoutで上限）。この二段構え（entry判定＋permit取得後の再判定）を片方だけにするとガードが素通りになる（`test_memory_guard_downgrades_second_lane_to_queue_without_failing`が実際に最初の実装のバグを捕まえた）。
- **孤児レーン回復は`WorkerSupervisor._recover_orphaned_generation_slot`と同じ論理積をper modelで。** 「レーンlock保持中」「ownerがactiveにもqueuedにも不在」「unowned active request不在」の全一致でのみ解放する。v1.6.2のプール即席キューにはこの機構が無く、handshake中に切断したclientがレーンを恒久的に塞げた。`new_request` / `queue_wait` / `capacity_check`の各所で評価する。
- **permit会計は`PoolSlot.gen_permit`で追跡。** `_release_lane`と`_recover_lane`はこのフラグが真のときだけ`_gen_slots.release()`と`_gen_active_lanes`減算をする。フラグを見ずにreleaseするとセマフォを過剰解放して並行上限が壊れる。
- **`generationConcurrency`はservice生存期にlatch。** 実行中にセマフォ容量を入れ替えると片側レーンが宙に浮く。`enabled`と同じ扱いで、`__init__`で読んで固定、変更は次回起動。
- **既定2はオーナー明示指示。** `common rules` / このメモの「実測がないなら既定値を動かさない」から意図的に外れている。合算allocationピークの実機計測は未実施（`TEST_PLAN_v1.7.0.md §2`）。次に実機を触れる人は必ずこの手順（2モデル常駐で同時ストリーム、RSS/`vm_stat`/`kern.memorystatus_vm_pressure_level`を1秒間隔で記録）を通し、結果をここに追記すること。問題が出たら既定を1へ戻す。
- **個別unloadは`POST /api/v1/models/{id}/unload`。** 既存の`DELETE /models/loaded`（全解放）は据え置き。CLIの`model unload`（引数なし）も全解放のまま。`_evict_slot`のlease guardは維持——生成中のWorkerをstreamの下から殺さない。`force=true`はcancel + unpin + best effortで、leaseが落ちた後の次回reapが回収する。
- **OpenAI互換入口は変えていない。** ルーティングは従来どおり`generate_for_model(str(loaded["id"]), ...)`。`_ensure_requested_model`が生成中の新規モデルautoloadを`ENGINE_BUSY`で拒むのは仕様どおり（生成中のモデル切替を避ける）。同時に使うモデルは事前に常駐させる前提。128 tools上限・bearer認証・SSE keep-aliveコメントは不変。

## v1.7.1で守ること（複数モデル表示 / エラー日本語化 / OpenAI互換）

- **メニューバーの複数モデル表示はバックエンド不変、表示層だけ。** `/api/v1/status`の`loadedModels`（配列、v1.6.2から存在。`poolState` / `memoryReservationBytes` / `activeLeases` / `keepLoaded` / `idleExpiresAt` / `memoryManagedBy`を含む）を`MenuBarViewModel.residentModels`へ復元し、`MenuBarView`で`residentModels.count > 1`のとき一覧表示する。単数`loadedModel`は今後も「プライマリ（直近使用）」でヘッダー専用。GUIは必ず`loadedModels`を読むこと——単数だけ読む実装へ戻さない。個別unloadボタンは`unloadModel(id)` → `POST /api/v1/models/{id}/unload`（既存の`unload_one`ルート）。`poolState == "external"`（LM Studio管理）の行はunload/pinを出さない。
- **エラー日本語化は「`code`契約 → クライアント側辞書引き」が一次。** `Sources/MLXBar/Services/ErrorText.swift`の`CoordinatorErrorText.table`（`code` → (ja, en)）で解決し、未知`code`はサーバ`message` → `code`の順にフォールバック。`ClientError`に`.api(code:message:)`を追加し、`CoordinatorClient.apiError(in:)`が`detail.code`と`detail.message`の両方を返す。`MenuBarViewModel.presentError(_:)`が全`catch`の共通経路。**新しい`MLXBarError` codeを足したら`ErrorText.swift`の`table`にも足すこと。**
- **サーバは今後も日本語`message`を返す責務。** `errors.py` / `api/*.py` / `Workers/common/server.py`のMLXBar由来エラーは`code` + 日本語`message`。上流ランタイム（mlx-lm / mlx-vlm / transformers）の英語例外は`message`を日本語の分類文言に置換し、原文は`detail`へ退避（GUIは出さず、`coordinator.log`に残る）。`make_management_app`のcatch-allは`message`固定・クラス名は`detail`へ。`RequestValidationError`も同様。**OpenAI互換のエラー本文の形（`{"error": {...}}`）と`code`値は不変**——`message`の中身だけ日本語化した。外部API利用者には従来どおり`code` + `message`が届く。
- **`max_tokens`省略時は`effective_max_tokens()`（＝`min(generation.maxTokens, modelMaxTokens)`、既定8192）。512ではない。** `api/openai_compat.py`の`chat()`で、`max_completion_tokens`も`max_tokens`も`None`のときだけこのフォールバック。明示値は従来どおりworker側`_validate_generation`が`min(値, effective_limit)`でclamp。`effective_max_tokens`を持たないworker実装（テストのダミー）では512へ`getattr`フォールバック。オーナー確認済みの意図的な既定変更（`common rules`の「実測がないなら既定を動かさない」に対する例外。エージェント系クライアントの応答途中終了への対応）。
- **単一常駐フォールバックルーティングは「常駐ちょうど1件」かつ`autoLoadOnAPIRequest`有効のときだけ。** `_ensure_requested_model`で、`find_loaded_model` / `loaded`のいずれにも一致せず`loaded_models()`が1件なら、その1件を返す（autoloadも`ENGINE_BUSY`もしない）。**常駐2件以上のときの解決ロジック（自動ロード / `ENGINE_BUSY` / `MODEL_NOT_FOUND`）は不変。** `getattr(workers, "loaded_models", None)`ガードで旧worker実装は従来動作。回帰: `test_an_unknown_model_is_missing_rather_than_busy_while_another_request_runs`（`make_autoload_client`は`loaded_models`無し＝対象外）、`_TwoModelPool`系（2件＝対象外）。
- **ストリーミングの`delta.role`は最初の1チャンクのみ（OpenAI準拠）。** `tool_call_delta`分岐にローカル`bool`フラグ。`_tool_call_stream_chunks`（`tool_calls`イベント経路）は元から最初のみroleなので、そこを通ったら同じフラグを立てて後続の`tool_call_delta`が再送しないようにする。
- **未知パス / `/v1/completions`はOpenAIエラー形式。** `main.py`に`StarletteHTTPException`ハンドラを追加（`fastapi.HTTPException`ハンドラは routing 由来のbare 404 を捕まえない）。`/v1/completions`は明示ルートで`404 UNSUPPORTED_ENDPOINT`。

## v1.8.0で守ること（同一モデルの並列常駐&生成 / Anthropic互換API）

### レプリカ（`models.pool.profiles[].replicas`）

- **`replicas`既定1のとき、`_slots`の内部キーは bare `model_id`（replica 0）。** `_slot_key(model_id, 0) == model_id`。既存の`_slots[model_id]`アクセスと全既存テストがbyte-identical。replica 1..Nだけ`f"{model_id}#{index}"`。ここを「常に`#0`」に変えると316件が壊れる（実際に実装中に踏んだ）。
- **replica 1..Nの`instance_key`は`f"{digest}-{index}"`。** replica 0は従来どおり素の`digest`（`sha256(model_id)[:12]`）。manifest（`control/worker-<key>.json`）・ログ（`logs/worker-<engine>-<key>.log`）が分離される。orphan回収は`_reap_pool_orphans`のglob（`worker-*.json`）が拾うので rename に強い。per-supervisorの`reap_orphan_worker`（厳密パス）はpool slotが`reap_orphans=False`なので無関係。
- **admissionはper-replica。** `_admit(model)`はレプリカ1個ぶんの`_cold_estimate`を審査し、`_resident_charge()`は全slotの予約合計。2個目のレプリカは自動的に「合計が予算内か」で判定される。4層（事前予約 / global load lock / allocator上限 / ロード後実測）は不変。`maxResidentModels`は**常駐Workerプロセス総数**の上限（意味を変えない）。`maxReplicasPerModel`（既定2）はper-model-idの上限で`_desired_replicas`がclampする。
- **コールドロードは直列のまま。** `_load_replica`が`_load_lock`を取る。`load()`はレプリカを`range(desired)`でループ（各回`_load_replica`）。`_desired_replicas`は`pin=True`のときだけプロファイル値、`pin=False`（API autoload）は1。差分は`_scale_up_pinned_replicas()`（reaper）が背景で埋める——**API要求に追加admissionのコストを払わせない**。
- **ルーティングは`_pick_replica(model_id)`。** ready かつ`gen_lock`非ロック・`gen_queued`空のレプリカを優先（キャッシュが温かい`last_released_at`最大）、無ければ`len(gen_queued)+locked`最小。選んだslotに対して既存のper-slotレーンロジック（`_acquire_lane` / `gen_lock` / プール`_gen_slots`セマフォ / `_concurrent_start_ok` / `_recover_lane`）をそのまま適用。プール全体の同時生成数は`generationConcurrency`が引き続き束ねる。`generationConcurrency=1`なら複数レプリカでも直列（`test_generation_concurrency_one_serialises_even_with_replicas`）。
- **スケールダウンは replica 0 と leased を除く高インデックスから。** `_reap_once`内。`session_pinned`は per-process pin ではなく容量設定なので、replicas 1..Nは`desired`まで trim してよい（replica 0 は常に残す）。
- **`unload_model(model_id)`は全レプリカを解放。** `snapshot_resident`はmodel_idで**dedupe**（`reload_resident`→`load()`が全レプリカを再作成するため、per-replicaでsnapshotすると多重ロードになる）。
- **`pool.enabled=false`はレプリカを完全無視**（`_legacy`単一Worker経路）。`replicas > 1`は`models.pool.enabled: true`必須。
- **既定値**: `replicas`既定1、`maxReplicasPerModel`は上限値（安全側）。`common rules`の「実測がないなら既定値を動かさない」に沿う。合算allocationピークの実機計測は未実施（`TEST_PLAN_v1.8.0.md §2`）。

### Anthropic互換API（`/anthropic`）

- **`openai_compat.py`は1行も変更していない。** 共通ロジック（`_ensure_requested_model` / `_find_model` / `_is_generatable` / `app_state`）は`anthropic_compat`が`from .openai_compat import`で直接使う。物理的な抽出（`shared_generation.py`）はしていない——回帰リスクを避けるため。`_ensure_requested_model`等の`_`接頭辞名を跨いでimportしているのは意図的。
- **sub-app。** `make_public_app`が`api.anthropic.enabled`真のとき`app.mount("/anthropic", make_anthropic_app(state))`。フラグはservice生存期にlatch（アプリは起動時1回構築）。`make_anthropic_app`は自前の例外ハンドラでAnthropic形（`{"type":"error","error":{...},"request_id":"req_..."}`）を返す。
- **`PublicRequestGuard`の`/anthropic`分岐。** 認証は`x-api-key`または`Authorization: Bearer`（`_authorized(headers, anthropic=True)`）。`anthropic-version`必須。早期リジェクトは`_reject_anthropic`でAnthropic形。**非`/anthropic`（OpenAI経路）の`_authorized`は`compare_digest(authorization, "Bearer "+token)`のままでbyte-identical**（`anthropic=False`のとき第2条件が`False and ...`）。`_reject`のOpenAIボディも不変。
- **出力は`AnthropicMessageBuilder`状態機械（`anthropic_stream.py`）。** content block indexは単調増加・再利用しない、開いたblockは必ず閉じる、`message_stop`は正常完了時のみ（ストリーム中errorは`event: error`で終端し`message_stop`を出さない）、`[DONE]`を出さない。`reasoning_delta`は**転送しない**（署名付きthinking blockを作らない。v1の既知の制限）。
- **`stop_sequence`。** `StopSequenceFilter.matched`にマッチ文字列を保持し、`server.py`の`completed`イベントに`stop_sequence`を追加（`stopped`真のときのみ）。**OpenAI経路は無視**（`finish_reason`は従来どおり`stop`）。Anthropic経路だけが`stop_reason: "stop_sequence"`へマップ。
- **偽装なし。** 応答`model`は`loaded.get("name") or id`。`usage`は`input_tokens`/`output_tokens`のみ（`cache_creation_input_tokens`等を出さない）。`cache_control`は受理して捨てる。未対応（server tools / thinking / document block / MCP）は`invalid_request_error`で明示400。
- **`count_tokens`はWorker RPC。** `BaseAdapter.count_tokens`既定は`NotImplementedError`→worker `/rpc`が501 `COUNT_TOKENS_UNAVAILABLE`。`ModelPoolSupervisor.count_tokens`が`getattr`ガードで古いランタイムを`COUNT_TOKENS_UNAVAILABLE`へ。概算を「正確な値」として返さない。未常駐時は`_ensure_requested_model`でautoload（`autoLoadOnAPIRequest`準拠）。
- **新しい`MLXBarError` codeを足したら`Sources/MLXBar/Services/ErrorText.swift`と`anthropic_stream._anthropic_error_type`の両方に追加すること。**

## v1.8.1で守ること（GUI操作のCLI完全対応）

- **`cli.py`の変更は追加のみ。** GUIの全ミューテーション（`MenuBarViewModel`の44メソッド＋設定画面のトグル）は管理APIを叩く。CLIも同じAPIを叩く。v1.8.1で「名前付きコマンドが無かった」ぶんを埋めた：`prompt-cache {status,clear-memory,clear-disk,set}`、`config set-model-pool`、`config set-flag`、`model set-replicas`、`lmstudio {set-base-url,set-auto-load}`、`model unload --force`の全解放パスへの転送。**必要なエンドポイントはすべて既存**——管理API/openai_compat/anthropic_compatのハンドラは1行も変更していない。
- **`model unload`（フラグなし・model_id省略）は`DELETE /api/v1/models/loaded`のまま。** `--force`を渡したときだけ`?force=true`を付ける（`--force`引数自体はv1.7.1から定義済み、no-argパスで無視されていたのを転送しただけ）。`test_cli::test_unload_all_without_force_is_unchanged`で固定。
- **`config set-model-pool` / `prompt-cache set`は部分パッチ。** 指定されたフラグのぶんだけ送る。`models.pool.profiles`や他の`promptCache`キーは送らないのでdeep-mergeで保持される（GUIの`setModelPoolSettings`/`setPromptCacheSettings`と同じ）。空指定と範囲外はクライアント側で`ValueError`（サーバ`_validate`が最終権威なのは不変）。
- **`config set-flag`の名前マップは5個だけ**（`auto-load-on-api` / `anthropic-api` / `remote-image-urls` / `require-token` / `continue-after-gui-exit`）。増やすときは`execute()`の`keys`辞書と`parser()`の`choices`を両方。汎用`config set <dotted.key> <json>`は`nested_patch`経由でGUI非露出キーにも届くエスケープハッチとして残す。
- **`model set-replicas`はロードを起こさない**（`model pin --replicas`はpin後に即ロードする）。GET settings → profiles更新（未pinなら`keepLoaded:true`で追加）→ PUT のみ。GUIの`setModelReplicas`と挙動を一致させている。
- **`SMAppService`（ログイン項目登録）はSwift専用のまま**（`## CLIとGUIの機能パリティにおけるOS API境界`）。`config set-launch-at-login`は設定値だけ、実登録はGUIが次回起動時にreconcileする。READMEの「GUI ↔ CLI 対応」表にこの1点だけ例外と明記。

### v1.8.0レビューの3件修正（v1.8.1同梱）

- **Anthropicストリームのinput token数**（`anthropic_stream.py`、追加のみ）: `handle()`/`stream_events()`の`metrics`分岐でも`prompt_tokens`を採用（実Workerは`usage`イベントを出さず`metrics`にだけ載せる）。`message_delta.usage`に`input_tokens`を含める。`message_start`は生成前概算のまま。
- **`COUNT_TOKENS_UNAVAILABLE`**: `_anthropic_error_type`で`invalid_request_error`→`api_error`（HTTP 503と整合）。
- **Swift設定画面**: `Stepper(in: 1...max(1, maxReplicasPerModel))`、pinプロファイル読み込みを`Dictionary(_:uniquingKeysWith:)`に。壊れたconfigでクラッシュしない。

## 未解決の課題

### tool無しリクエストで推論ブロックが本文へ漏れる（v1.8.4対応）

**症状はモデルではなくリクエストの形で決まる。** 同一の重み `Ornith-1.5-35B-A3B-MLX-4bit`（mlx-lm）で、`tools` を含まないリクエストだけ `content` に `The user is asking me to…\n</think>\n\nPONG` のように生の思考文＋閉じ `</think>` が漏れ、`tools` ありは stream / non-stream とも正常。LM Studio 直（:1234）も正常。Qwen3.8 は `tools` 無しでも漏れない（テンプレートの `enable_thinking` トグルを尊重して黙るため）が、Ornith 1.5 は `enable_thinking=false` / `/no_think` / `reasoning_effort=none` をすべて無視して常に推論ブロックを出すので露出する。OpenClaw の `tools` を付けない補助呼び出し（memory dreaming、タイトル生成、compaction 要約、パイプラインの整形ステップ）が実害面。

**原因は `Workers/common/server.py` の `tool_mode` ゲート。** `<think>` を切り分ける `IncrementalToolStream` が `tool_stream = IncrementalToolStream() if tool_mode else None` で、`tools` なしだと `None`。`reasoning_start` 処理・`delta` の分離・末尾 `finish()` フラッシュがすべて `tool_mode` 条件下。`tools` 無し経路（`elif event.get("type") == "delta":`）は生パススルーで、推論文も閉じタグもそのまま `content` へ。v1.2.0 の `parse_tool_markup` 側除去は `adapter.finalize` 経由＝`tool_mode` 内でしか走らず、かつ `<think>` と `</think>` が両方揃っているときにしか中身を消せない。テンプレートが先頭 `<think>` をプロンプト側で開くモデルでは出力に閉じタグしか出ないため、正規表現ではタグだけ消えて推論文が残る。

**修正: 切り分けを `tool_mode` 非依存にする。** `IncrementalToolStream(reasoning_only=True)` を追加（`<think>`/`</think>`/`<assistant>` のみ、`<tool_call>` 系は一切特別扱いしない）。`server.py` で `tools` の有無にかかわらず常に構築し、`reasoning_start` と `finish()` フラッシュを `tool_mode` の外へ出す。`adapter.py`（mlx-lm / mlx-vlm 両方）の `reasoning_start` 発火条件（`prompt.rstrip().endswith("<think>")`）からも `tool_mode` ガードを外す。Coordinator（OpenAI / Anthropic、stream / non-stream）は無変更（`reasoning_delta` は既に stream で `reasoning_content` へ、non-stream と Anthropic では破棄）。

**不変条件:** `tools` あり経路は `reasoning_only=False`＝従来と同一。`tools` 無しで自発的に `<tool_call>` を吐くモデルは従来どおり本文として可視（`TOOL_PARSE_FAILED` は増えない）。非推論モデルは末尾 `<` 由来の最大 11 文字・1 デルタの保留を除きバイト等価。

**意図した挙動変更:** `tools` 無しで推論モデルへ問い合わせると `<think>` の中身は `reasoning_content`（stream）へ分離／破棄（non-stream）され `content` へ出ない。

**検証:** Python 373 件（367 + 新規 6）。実機は稼働中の mlx-bar（Ornith 1.5 35B 常駐）へ `tools` 無し合成リクエスト。

### v1.6.0のprefix再利用が実機で一度も有効になっていなかった（v1.6.1で対応）

**出発点は「テストが通っているのに、実機のAPIログが全部coldになる」**。v1.6.0の37件のテストは全部緑で、実機の191件の生成のうち`cold_reason`が入っている行はゼロ、`shared_prefix_tokens`が0でない行もゼロだった。**その2つが同時に起きているとき、疑うのは機能ではなく計装である。**

**判定は「型」に聞くこと、「値」に聞かないこと。** `can_capture()`が`hasattr(entry, "state")`でインスタンスを読んでいた。能力プローブはロード直後の**空の**キャッシュに対して走る（重みが要らないので安い）が、空の`KVCache`は`state`ゲッターが`self.keys.shape[2]`を読むため`AttributeError`になり、`hasattr`はそれを`False`に変換する。結果、注意層と再帰層が混在するモデルはすべて`rollbackCapability: none`。**v1.6.0の主機能は一度も起動していない。** `getattr(type(entry), "state", None)`がpropertyでfsetを持つかだけ見れば、空でも同じ答えが出る。

**mlx-vlmは`.cache`を2回読む。** `dispatch.py:844`の`if prompt_cache_state is not None and prompt_cache_state.cache is not None:`が1回目、`846`の`kv_cache = prompt_cache_state.cache`が2回目。v1.6.0の順序ガードは「まだ誰も読んでいないときだけ差し替える」で、1回目を数えてしまうため**復元経路に一度も入らない**。v1.5.0のメモにある「`find_prefix_length()`は`.cache`読み出しより前に呼ばれる」は1行ずれていた。**上流の行番号をメモに書くときは、その行の前後も一緒に読むこと。**

**差し替えではなく中身の入れ替えにすれば、順序に依存しなくなる。** 保持しているリストを`clear()`して`extend()`する。呼び出し側が先に参照を取っていても、その参照が復元後のキャッシュになる。ついでに`.cache = None`をやめられる——ランタイムは自分のnull検査を通過済みで、その後に渡されたものを反復して破棄量を計算するので、`None`を返すと`TypeError: 'NoneType' object is not iterable`になる。**v1.6.0にこの地雷が埋まっていたが、復元経路に到達しないので踏めなかった。順序ガードだけ直すと露出する。**

**ランタイムは破棄量を「返した長さ」ではなく「キャッシュのoffset」から計算する。** `_prefix_cache_trim_amount()`は`max(offset) - prefix_len`。したがってキャッシュが自分のラベルより先行していると、**単なる継続でも**`trim()`に届く。事前判定は「巻き戻しが要るか」ではなく「**返す長さが破棄を生まないか**」で書くこと。稼働中のWorkerで14件記録されていた。

**`mx.save_safetensors`は拡張子で終わらないパスに`.safetensors`を足す。** `<digest>.safetensors.tmp`へ書くと`<digest>.safetensors.tmp.safetensors`ができ、直後の`chmod`がENOENTで落ちる。読めないスナップショットと214 MBの残骸だけが残る。一時ファイル名は**拡張子で終わらせる**（`<digest>.tmp.safetensors`）。**偽の`mlx.core`は言われたとおりの場所へ書いていたので、単体テストでは出ない。** 偽モジュールは実物の癖まで真似ること。

**APCの世代掃除とCheckpointStoreの保存先が同じ親を共有していた。** `apc_root.iterdir()`のうち現在のnamespace以外を古い順に消す実装で、`checkpoints/`もその対象に見える。世代が2つ以上ある状態で、いま使っているモデルのスナップショットごと消えうる。掃除の対象は名前の接頭辞（`mlxbar-vlm-v1-`）で限定する。

**計装は「列がある」ではなく「端から端まで届く」でテストする。** `cold_reason` / `shared_prefix_tokens` / `held_prefix_tokens`は、列もmigrationもWorkerもUIもv1.6.0で揃っていたが、`main.py`のアクセスログが組み立てる辞書にだけ載っていなかった。DB層の単体テストは通る。**Workerのイベント→APIログ→DBを1本で通すテストを書くこと**（`test_the_workers_cache_report_reaches_the_api_log`）。再試行が走ると`last_probe`が差し替えで消えるので、probeは`_reset_prompt_cache()`の前に控えておく。

**まだ残っている問題**

- **同一モデルを複数クライアントで共有すると、キャッシュを取り合う。** `PromptCacheState`もCheckpointStoreのメモリ層も**1本しか持たない**。LANのエージェントとローカルのテストを交互に流すと、双方が毎回相手のprefixを追い出して両方coldになる。実測で確認済み（LAN側が77,127 tokenのprompt、共通prefixは相手の925 tokenだけ）。会話ごとにスロットを持つ設計が要るが、27B級のキャッシュを複数保持するメモリ予算と直結するので、`memoryRatio`の設計とセットで考えること。
- **`/api/v1/prompt-cache`はWorkerのMLXスレッドを待つ。** v1.6.1で「生成中は`stale: true`で既知の値を返す」ようにしたが、根本は「読み取り専用のRPCが生成と同じスレッドで直列化されている」こと。統計だけ別スレッドで答えられるようにするなら、`apc_manager.stats_snapshot()`がMLX配列に触らないことを先に確認すること。
- **OpenClawの無応答監視はSSEコメントを数えない。** MLXBarのheartbeatはコメントなので届かない。可視イベント（content / reasoning / tool call）だけがタイマーをリセットする。prefill中は送れる本物のデータが無いので**MLXBar側では塞げない**。READMEの手順で`models.providers.<id>.timeoutSeconds`を指定してもらうしかない。空の`delta: {}`も効かないことを実測で確認済み。

### 中断後の再開とtrim非依存の再利用（v1.6.0対応）

**当初の想定が外れた点から書く。** v1.5.0の「次にやるなら」には「部分ロールバック（prefill完了時点へのtrim）ができれば」と書いたが、**この方向は原理的に行き止まりだった。** `Qwen3.8-27B`は64層中48層が`linear_attention`で、その状態は`ArraysCache`＝固定サイズの漸化式状態。「末尾Nトークンを削る」という操作が数学的に存在しないので、`trim`相当のAPIをどこに足しても解けない。**巻き戻しではなく複製と復元（checkpoint）が唯一の解**である。

**依拠したランタイム事実（mlx-vlm 0.6.15で確認、いずれも`getattr`/`hasattr`で存在確認してから使う）**

| 事実 | 位置 |
|---|---|
| `GenerationResult.token`がトークンIDを持つ | `generate/common.py:207` |
| `ArraysCache.state`にgetterとsetterがある（＝trim不可でも複製・復元は可能） | `models/cache.py:638` |
| `find_prefix_length()`は`.cache`読み出しより**前**に呼ばれる | `generate/dispatch.py:845-846` |
| `update(all_ids, tracked_cache)`は**完走時のみ** | `generate/dispatch.py:1062` |
| `all_ids = full_input_ids_list + generated_tokens` | `generate/dispatch.py:1063` |
| キャッシュは`model.language_model`から作る | `generate/dispatch.py:934` |

最後から2番目が中断時破棄の直接の原因である。中断するとキャッシュだけが進み、`token_ids`は前ターンのまま取り残される。

**プロンプトのトークンIDを取る唯一の方法**: ランタイムは`stream_generate`の内部でトークナイズするので、外からは見えない。**出力テキストの再エンコードは絶対にやらないこと**（トークナイザは往復同一性を保証しない）。`PromptCacheState`を継承して`find_prefix_length(new_ids)`を上書きすると、ランタイムが計算した`full_input_ids_list`がそのまま渡ってくる。ここが唯一の入手経路。

**書き換えてよい条件は「証明できるとき」だけ**: `max(getattr(c, "offset", 0))`が`len(prompt_ids) + len(generated_ids)`と一致することを必ず確認する。cold経路ではランタイムが別のキャッシュを作るため保持キャッシュは動いておらず、この検査だけで自動的に弾ける（「再利用されたか」の別フラグは不要）。

**トークンIDはキャンセル検査より先に集める**: 実装中に一度踏んだ。`for response in stream_generate(...)`で「キャンセル検査 → トークン収集」の順にすると、キャンセルが見えたイテレーションの応答を数え損ねる。ランタイムはその応答を作るためにキャッシュを進めているので、**キャッシュは1手先**になり、上の検査が（正しく）拒否して機能しなくなる。テキストがクライアントへ届いたかは無関係で、モデルが処理したかだけが問題。

**`trim`到達を防ぐ方法**: `find_prefix_length()`が**0を返す**と、dispatch側の`0 < prefix_len`が偽になり再利用分岐ごと飛ばされる。巻き戻しが必要（`prefix_len < 保持長`）かつ`hasattr(c, "trim")`が偽のときに0を返せば、例外そのものが起きなくなる。v1.5.2の例外検出は安全網として残してある。

**`.cache`差し替えの安全策**: スナップショット復元は`find_prefix_length`の中で`.cache`と`.token_ids`を差し替えて実現している。dispatchが`.cache`を後に読むという**呼び出し順に依存する**ので、`.cache`をpropertyにして「今回の要求で既に読まれたか」を数え、読まれた後は差し替えない。将来dispatchが先に読む実装になっても、返すprefix長とcacheが食い違わない。

**容量は必ずconfigから算出する**: `Qwen3.8-27B-MLX-8bit`は`16層 × 2(K,V) × 4 kvヘッド × 256 × 2byte = 64 KB/トークン`。10万トークンで6.4 GB。**`diskMaxGB: 5`では1件も入らず、書いては捨てるだけになる**（実機で4.2 GBの残骸を確認）。既定の10 GBなら約161,000トークンまで入る。固定GB値は次のアーキで必ず破綻する。`scripts/cache-capability-matrix.py`で重みをロードせずに全モデル分を一覧できる。

**スナップショットは完了ターン末で取る**: prefill完了の瞬間（＝キャッシュがプロンプト末尾と一致する点）にはフックが無い。最初の応答が来た時点でキャッシュは既に`prompt+1`まで進んでいる。一方、**完了ターン末の状態は、線形な会話における以降すべてのプロンプトのprefixになる**（次のプロンプト＝このターン＋新しいメッセージ）ので、そこで取れば十分かつタイミングの機微が無い。

**メモリのピークに注意**: `remember()`は新しいpayloadを作る前に古いpayloadを解放すること。両方持つとライブのキャッシュと合わせて3倍になり、長い会話では十数GBになる。

**書き込み量**: 1スナップショット6.4 GBを毎ターン書くと1日で数百GB。増分（前回比1.25倍または+8192トークン）でしきい値を設け、`promptCache.diskWriteBudgetGB`（既定32）で上限も持つ。

**中断の種類で扱いを変える**: キャンセルは保持、`MEMORY_PRESSURE`は解放。同じ「未完了」でも目的が逆。Worker側は`BaseAdapter.note_abort()`で理由を受け取る。**`close_on_mlx_thread()`は投げっぱなしなので、リクエストの`finally`で理由を消してはいけない**（adapterの`finally`はその後に走る）。registryは期限と件数で自分を縛る。

**`maxPromptCharacters`は上限ではなく下限だった（誤解の記録）**: 実測で設定100,000に対し968,524文字の要求が200で通っていたので疑ったが、`effective_max_prompt_characters()`が`max(configured, min(model_max_tokens * 4, 10_000_000))`を返す仕様。`max_position_embeddings: 262144`のモデルでは約1,048,576文字になる。**バグではない。** 名前が実態と合っていないので、次にUIを触るときに説明を足すこと。

**上流**: `mlx_vlm/generate/dispatch.py:849`の`_prefix_cache_trim_amount()`が`_cache_fully_retained()`だけで守っており、`trim`を定義しない型を通す。`hasattr(c, "trim")`を足せば全ハイブリッドが一度に直る。**MLXBarはもう依存していないが、直れば能力プローブが自動的に安い経路へ戻す。**

**実装しなかったもの（理由つき）**
- **モデル別のwarm保持** — supervisorの単一モデル前提を崩す必要があり、中断復帰という目的に対して割に合わない。
- **`promptCache.memoryBlocks`の既定有効化** — APCのブロックプールは27B級ハイブリッドでの計測が無い。設定と算出だけ用意し、既定はoffのまま。有効化を検討するなら、`APC_NUM_BLOCKS`を変えた状態で2-2のJとKを回して、first tokenとピークメモリの両方を見ること。

### 公開APIの認証前リソース消費（v1.5.3で対応）

**実測の推移**: 認証なし40MB×24並列で、RSS 60→1,822 MB（v1.5.2）が 60→69 MB（v1.5.3）になった。

**要点**: FastAPIはハンドラ引数`body: dict`をハンドラ本体より先に解釈する。`authorize()`をハンドラの中に置くかぎり、資格情報の検査は要求全体を読んだ後にしか走らない。**壊れたJSONを認証なしで送ったとき401ではなく422が返るかどうかが、この順序の判定材料になる。**

**実装**: 純ASGIミドルウェア`PublicRequestGuard`。`add_middleware`は最後に登録したものが最外になるので、アクセスログ用ミドルウェアより**先に**登録して内側へ置くこと（そうしないと401/413がログに残らない）。ミドルウェアからは`scope.setdefault("state", {})["api_log"]`へ書けば、外側の`request.state.api_log`から読める。

**上限を固定値にしてはいけない**: `maxImages` 8 × `maxImageBytes` 25 MiB × base64 4/3 ＝ 約280 MBが**正当な**要求サイズ。固定10MBのような上限を入れると画像入力が壊れる。設定から算出すること。

**チャンク送信**: `Content-Length`が無いので事前拒否できない。`receive`をラップして累積バイトで打ち切る。実測では上限がそのまま効き（上限10MBで419MB送信→RSS +33MB、上限281MBで+707MB）、uvicorn側が残りを溜め込むわけではないことを確認した。

**この判定は全要求を通る**: 設定キー欠落で500にしないこと。認証を先に評価し、上限計算は`.get()`とtry/exceptで防御する（テスト用の部分的なsettingsスタブで実際に500を踏んだ）。

### 生成中のトークン毎秒（v1.5.3で追加）

- **`delta`を数えてはいけない。** ランタイムはdetokenizerが区切りを得たときだけ`delta`を出す。実機`Qwen3.5-9B-MLX-8bit`は111トークンを14秒ため込んでから**1つの**`delta`で吐いた。delta基準の実装では14秒間なにも表示できない。生成ループ側の刻み（`token_progress`）で計測すること。
- **値はランタイムから取れる。** mlx-lm・mlx-vlmとも各`GenerationResult`に`generation_tokens`と`generation_tps`を載せており、tpsはprefillを除外した最初のトークンからの累積平均。独立計測と一致することを確認済み（66.09 vs 66.12）。`getattr`＋フォールバックで読むこと。
- **初回サンプルは捨てる。** `(n+1)/経過`で経過≈0のため、n=0では57,280 tok/sのような値になる。3トークンに達するまで公開しない。
- **`last_visible_event`を触らないこと。** あれはtool_parse heartbeatのタイマーで、クライアント接続の維持が目的。進捗で更新すると既存のheartbeatを飢えさせる（実際にテストが落ちた）。
- **テストのタイミング**: `SlowBufferedToolAdapter`のsleepは heartbeat間隔より**短く**保つこと。長くするとdelta間にprefill heartbeatが挟まって`last_visible_event`が更新され、tool_parseの判定が競合になる。「余裕を持たせるつもりで長くしたら不安定になった」を一度踏んでいる。

### mlx-vlmのプロンプトキャッシュ巻き戻し失敗（2026-08-22対応・v1.5.1）

**症状**: Qwen3.8-27B等で、会話が枝分かれしたターンだけ`GENERATION_FAILED` / `'ArraysCache' object has no attribute 'trim'`。

**原因はMLXBarではなくmlx-vlm 0.6.15。** `generate/dispatch.py:859`が保持キャッシュを共通prefixまで巻き戻す際、`c.trim(n_drop)`を`is_trimmable()`ではなく`_cache_fully_retained()`で守っている。後者は`caches` / `start_position` / `max_size`のどれも持たない型に対して最後の`return True`へ落ちるため、`trim`メソッド自体を持たない`ArraysCache`（qwen3_5のlinear_attention層）を通してしまう。`is_trimmable()`は正しく`False`を返している。

**再現条件**: (1)`prompt_cache_state`を渡している（v1.3.7以降のMLXBarは常に渡す）、(2)前要求のキャッシュ長 > 新要求の共通prefix長、つまり**会話の枝分かれ**、(3)モデルのキャッシュ配列に`ArraysCache`が含まれる。前ターンをそのまま続ける要求では起きないので断続的に見える。

**MLXBar側の対処**: `_is_cache_reuse_failure()`でtracebackに`mlx_vlm/generate/dispatch`か`mlx_vlm/models/cache`があるかを見て、応答未送信なら新しいキャッシュで一度だけ再試行する。失敗はprefill前なのでコストはほぼゼロ。APC由来の失敗と違い`apc_manager`は落とさないので、再試行はdisk hitを拾える（実測1,174 tokens再利用）。

**調査時の教訓**: 「v1.5.0を出した直後に出たエラー＝v1.5.0が原因」ではない。API logsに同じ`model` / `error_code` / `prompt_tokens=0`の組み合わせが前日にも記録されていたのが最初の手がかりで、最終的にv1.4.1のcheckoutに対して同じ3要求を流して再現させ、退行ではないことを確定させた。**バージョン間の切り分けは、旧versionのWorkersディレクトリを`sys.path`へ入れて同じスクリプトを走らせるのが速い。**

**小さいモデルで再現できる**: 27B（27 GB）を待つ必要はない。`Qwen3.5-9B-MLX-8bit`が同じ`qwen3_5` hybrid構成（`layer_types`に`linear_attention`）なので9 GBで再現する。`config.json`の`text_config.layer_types`を見て同系統を選ぶこと。

**上流**: `_cache_fully_retained()`と併せて`is_trimmable()`を確認すれば直る。未報告。

**v1.5.1の判定は広すぎた（v1.5.2で修正）**: 最初は「tracebackに`mlx_vlm/generate/dispatch`があるか」で判定したが、**`stream_generate`自体がそのdispatch.pyで定義されている**ため、生成中のあらゆる例外が一致してしまった。結果、通常のモデルエラーでもprefillを1回捨て、再試行前の`_reset_prompt_cache()`で暖まったキャッシュまで失っていた。判定は**モジュールではなく失敗した呼び出し**を見ること — tracebackの各行を`linecache`で読み、`.trim(`を含む行だったときだけ該当と判断する（行番号はランタイム版で動くので使わない）。

**テストの落とし穴**: この欠陥を最初のテストが見逃したのは、偽の`mlx_vlm`モジュールを`ModuleType`で作り、`stream_generate`をテストファイル内に定義していたため。**ランタイムのファイルパスに依存する判定は、偽モジュールでは検証できない。** 一時ディレクトリに`.../mlx_vlm/generate/dispatch.py`を実際に書き出し、`compile(..., str(path), "exec")`でそのパス由来のtracebackを作ること。

### v1.5.0 設計精査（2026-08-22対応）

27BクラスをZCodeから常用する前提で全体を精査し、17件を修正した。以下は次回以降に効く判断と、精査中に判明した誤認の記録。

**精査中に覆った仮説（同じ間違いを繰り返さないため）**

- 「切断してもWorkerが最大13分生成を続ける」は誤り。`httpx.ASGITransport`では`break`してもアプリ側タスクが走り続けるため完走するが、実uvicornはクライアント切断でレスポンスタスクをcancelするので生成自体は止まる。**Worker側の切断挙動をASGITransportで測ってはいけない。** 実際に残っていた欠陥は「adapterの生成器が閉じられず`finally`が走らない」＝中断時のプロンプトキャッシュ破棄が実行されないことで、性能ではなくキャッシュ整合性の問題だった。
- 「テキスト専用Qwen3はmlx-lmへ分類されプロンプトキャッシュが効かない」は、`Qwen3.8-27B-MLX-8bit`には当てはまらない。同モデルは`vision_config`と`image_token_id`とprocessor設定を持つ真のVLMで、正しくmlx-vlmへ分類される。mlx-lmのキャッシュ欠落は実在の欠陥だが、27B常用への影響はなかった。**モデルの経路はカタログの分類結果か`/api/v1/status`の`loadedModel.engine`で確認すること。**
- 「`<function=`形式がストリームへ漏れる」は誤り。`parse_tool_markup`は`<tool_call>`ブロックの内側しか見ないため、その記法単体では解析されない。実際に漏れていたのは`<|tool_call_start|>`、`<minimax:tool_call>`、`<atem:function_calls>`など**mlx-vlmの`tool_parsers`が解釈する別dialect**で、`IncrementalToolStream.TOOL_START`が`<tool_call>`単体だった。ランタイムのtool parser一覧は`mlx_vlm/tool_parsers/`を直接見て同期させること。

**設計判断のメモ**

- `resource.getrusage().ru_maxrss`は**高水位で、下がらない**。メモリ判定に使うと一度の大きなprefillで以後の要求が恒久的に拒否される。現在値は`/bin/ps -o rss=`から取る（2秒キャッシュ）。macOSに`SC_AVPHYS_PAGES`はないため空きメモリも`vm_stat`から取る。`kern.memorystatus_vm_pressure_level`（1/2/4）はOS自身の判定なので最優先で見る。
- mlx-lmの`LRUPromptCache.fetch_nearest_cache(model, tokens)`の第1引数は**モデルオブジェクトではなくハッシュ可能なキー**。`nn.Module`を渡すと`unhashable type: 'Model'`で毎回失敗し、RAM層が黙って無効になる（実際に一度踏んだ）。mlx-lm本体の`server.py`も`model_key`を渡している。
- ディスクへ保存するprefixは必ず**プロンプト長で頭打ちにする**。`prompt + generated - guard`で計算すると、`max_tokens`が大きいときモデル自身の応答までprefixに入り、以後どの要求とも一致しない1 GB級のsnapshotを書き続ける。
- adapterから`{"type": "completed"}`を出してはいけない。`server.py`の`lines()`がtool_callsとmetricsを出す**前**に転送されるため、クライアントは`finish_reason: stop`を受けてからtool callを受け取ることになる。finish_reasonは`metrics`イベント経由で`lines()`へ渡し、`lines()`が最終的な`completed`を組み立てる。
- 切断時の`iterator.close()`は`await`せず`mlx_executor.submit()`で**投げっぱなしにする**。`finally`がCancelledErrorを巻き戻している最中の`await`は次のawaitで再度CancelledErrorになり、closeがスキップされうる。submitならキューに載ることが保証され、単一ワーカーが実行中の`next_event`の直後に走る。
- APIログの剪定を毎回のINSERTで行うと、全要求が`ORDER BY`つきのDELETEを踏む。100回に1回へ間引き、保持件数は「上限」ではなく「目標」として扱う（テストもその前提で書き直した）。

**次にやるなら**

- ~~mlx-vlmの中断時プロンプトキャッシュは今も全体破棄~~ → **v1.6.0で対応。** ただし当時の想定（「prefill完了時点へのtrim」）は実現不能だった。理由と実際の解法は下の「中断後の再開」節を参照。
- 新設定（`wiredLimitRatio`、`cacheLimitRatio`、`promptCache.keepGenerations`、`promptCache.memoryRatio`）はまだGUIに露出していない。`config.json`直接編集か`mlxbarctl`のみ。
- 27B実機でのメモリ上限到達と長時間連続運転は未実施。

### ZCodeのlarge tool prefixによるcold prefill遅延（2026-08-21対応）
- 実機証拠: v1.3.6の直近ZCode要求は`stream=true`、messages 4件、tools 23件、HTTP 200で33.124秒。制御実験では同じQwenをthinking無効・toolsなしでfirst content 0.946秒、23 tools付きで7.506秒となり、thinking強度ではなくtool promptのprefillが差の中心だった。
- 根本原因: v1.3.6は生成開始後の全量バッファを解消したが、各OpenAI Chat Completions要求を独立生成として`mlx_vlm.stream_generate`へ渡し、ZCodeが毎ターン再送するsystem prompt・tools schema・履歴の同一prefixも毎回先頭から計算していた。27B 8bitの実tool schemaは大きく、first token前のcold prefill中はSSE keep-aliveしか返せない。
- 修正: mlx-vlm 0.6.15公式の`PromptCacheState`をWorkerのモデル寿命に合わせて1個保持し、テキスト要求の最長共通token prefixを再利用する。キャッシュは直前系列1件だけなので無制限に増えない。OpenAI入力のmessages/toolsやモデル出力は変更しない。
- 安全境界: 画像のplaceholder tokenは画像内容の同一性を保証しないため画像要求へキャッシュを渡さない。生成キャンセル・例外時はin-place更新された途中キャッシュを破棄する。キャッシュ保持でメモリ比率が安全上限へ達した場合は`clear_prompt_cache` RPCで解放後に再測定し、それでも高い場合だけ`MEMORY_PRESSURE`を返す。古いmlx-vlmに`PromptCacheState`がない場合は最適化なしで継続する。
- 可観測性: APIログへ本文・tool定義・キーを保存せず、文字数、first-token時間、prompt/cached tokens、prompt/generation TPS、推論モードだけを追加。既存SQLiteには追加列を自動migrationする。
- 推論強度dialect: 汎用テンプレートとの互換性を保つためOpenAIの`high` / `minimal`を原値で先にrenderし、失敗時だけQwen相当の`xhigh` / `low`を再試行する。明示された`none`は従来どおりthinking無効化へ変換する。
- 実モデル評価: 23 tools・55,881 schema文字・10,677 prompt tokensのcold要求はfirst delta 35.253秒／total 35.299秒。会話を1ターン延長したwarm要求は10,688 cached tokens、first delta 0.399秒／total 0.406秒、prompt TPS 308.92→36,911.43で約88倍のTTFT改善。モデルロード直後や共通prefixがない最初の要求は原理上cold prefillが残る。

### ZCode tool calling時の無表示・全量バッファ（2026-08-21対応）
- 実機証拠: v1.3.5のZCode要求はHTTP 400ではなく200になったが、`stream: true`、ツール20件、会話履歴最大236件で、完了まで42〜519秒を要した。Workerクラッシュも1回記録。最小のQwen＋thinking＋1 tool要求でも、空の初期チャンクとkeep-aliveだけが55秒続き、本文が完了時に一括到着した。
- 原因: [server.py](Workers/common/server.py)がtools有効時の全`delta`を`buffered`へ追加し、生成完了後のtool parserまで外部へ出していなかった。クイックチャットはtoolsなしのため逐次配信され、症状差と一致する。
- 修正: 増分フィルターが通常本文とreasoningを即時イベント化し、分割され得る制御タグの短い接尾辞だけを保留する。`<tool_call>`を検出した時点から解析用に保持し、確定したcallだけをOpenAI形式へ変換する。thinkingは`reasoning_content`へ分離し、解析失敗は明示エラーにする。
- 署名: runtime Pythonが同梱Workerソースへ`__pycache__`を作り、起動後にsealed resource追加として署名検証を壊していた。全Coordinator/Worker起動経路へ`PYTHONDONTWRITEBYTECODE=1`を設定する。
- 検証: 実機で観測したQwenのthinking終端とtool markerの分割パターンを含む回帰テストを追加し、Pythonテスト148件とSwiftリリースビルドが成功。

### ZCode互換拡張の将来互換化（2026-08-21対応）
- 症状: v1.3.4でトップレベルの`thinking`を追加した後も、実機のZCodeからQwen3.8へ送った要求がHTTP 400 `UNSUPPORTED_PARAMETER`になった。インストール済みアプリと配布DMGのCoordinatorハッシュは一致しており、v1.3.4未適用ではなかった。
- 原因: OpenAI互換入口、`extra_body`、`thinking`の各階層に未知項目を拒否するホワイトリストが残っており、ZCodeのバージョンやモデル別dialectによる追加項目で再発できる設計だった。旧監査ログは早期入力エラーのモデル名とエラーコードも記録できず、ZCode側も`parameters`配列を表示しないため、実機の拒否項目名を後から特定できなかった。
- 修正: 未知の互換拡張はWorkerへ渡さず無視し、既知の値だけを検証・正規化する。`extra_body.thinking`、`extra_body.reasoning_effort`、`thinking.effort`も受理する。MLXBar自身が管理する予約済み`chat_template_kwargs`は引き続き拒否する。
- 診断: API処理の先頭で本文を含まない監査メタデータを作り、HTTP入力エラーのモデル名とコードをSQLiteへ、拒否項目名だけをCoordinatorログへ残す。会話本文、応答本文、APIキーは保存しない。
- 検証: ZCode互換拡張と診断ログの回帰テストを追加し、Pythonテスト144件が成功。

### ZCodeのトップレベル`thinking`・`reasoning_effort`対応（2026-08-21対応）
- 症状: v1.3.3で`extra_body.chat_template_kwargs`に対応した後も、ZCodeがQwen3.8の要求にトップレベルの`thinking`を追加すると、Coordinatorのホワイトリスト検証がHTTP 400 `UNSUPPORTED_PARAMETER`を返していた。さらに`reasoning_effort`は許可項目にあるが、生成`options`へ入れておらず実質的に無視していた。
- 修正: `thinking`をブール値またはオブジェクトとして検証し、`type: enabled|adaptive`を`enable_thinking: true`、`type: disabled`を`false`、`budget_tokens`を`thinking_budget`、`clear_thinking`を逆値の`preserve_thinking`へ変換。トップレベルの`reasoning_effort`も小文字化して同名のテンプレート引数へ渡し、`none`の場合は明示値がない限りthinkingを無効化する。
- 優先順位: モデルに直結する既存の`extra_body.chat_template_kwargs`を最優先し、トップレベルの`thinking`と`reasoning_effort`は未指定値だけを補う。ZCodeが新旧両形式を同時に送っても、明示的なテンプレート設定が意図せず上書きされない。
- 検証: ZCode形式の正規化、明示値の優先、`reasoning_effort: none`、不正なtype・budget・履歴保持値を追加し、Pythonテスト142件が成功。

### ZCodeの`extra_body.chat_template_kwargs`対応（2026-08-20対応）
- 症状: ZCode 3.2.5が`POST /v1/chat/completions`に`{"extra_body":{"chat_template_kwargs":{"enable_thinking":false}}}`を追加すると、Coordinatorが`extra_body`を未対応パラメータと判定してHTTP 400 `UNSUPPORTED_PARAMETER`を返していた。モデルロードや量子化、生成Workerに到達する前のAPI入力検証が原因。
- 修正: [openai_compat.py](Coordinator/mlxbar/api/openai_compat.py)で`extra_body.chat_template_kwargs`を検証・抽出し、Worker生成オプションへ追加。[tool_calls.py](Workers/common/tool_calls.py)のテンプレート引数フォールバックの各段階で値を保持し、mlx-lmの`processor.apply_chat_template`とmlx-vlmの`prompt_utils.apply_chat_template`の両方へ届ける。
- 境界: `chat_template_kwargs`はモデル固有拡張のため任意のJSON値を伝播するが、MLXBarのAPI処理と衝突する`tools`、`tool_choice`、`tokenize`、`add_generation_prompt`、`num_images`は予約済みとして拒否する。`extra_body`内の未対応項目も従来の厳密なAPI検証に合わせて拒否する。LM Studio経路はMLXテンプレートを使わないため伝播対象外。
- 検証: API正常系・不正系、両Workerへの伝播、tool callingフォールバック時の保持をテストし、Pythonテスト138件が成功。Swiftは変更していないが、互換SDKを明示したリリースビルドも成功。

### 設定画面（Settings）の横幅が広すぎる
- APIサーバーページなど、コンテンツが約650pxしか使っていないのにウィンドウ幅が約900pxあり、余白が目立つ。この「無駄な余白」自体はv1.2.3でも未解消（モデルタブの横はみ出し・見切れの方を修正した）。
- 次に試すなら: `NavigationSplitView`をやめて`HSplitView`やカスタムレイアウトに置き換える方向で検討する。

### SwiftUIの既知の罠（踏まないよう記録）
- `NavigationSplitView`のdetailペイン内のテキストに`.fixedSize(horizontal: false, vertical: true)`を追加すると、**無関係のサイドバー（項目一覧）が空白表示になる**現象を複数回・複数パターンで再現した（[Sources/MLXBar/Settings/MLXBarSettingsView.swift](Sources/MLXBar/Settings/MLXBarSettingsView.swift)）。v1.2.3でモデルタブの説明文を折り返させる際に同じ現象を実際に踏んだ（サイドバーが空白化）。**`.fixedSize`を使わず、`.frame(maxWidth: .infinity, alignment: .leading)`だけで折り返しを実現する**ことで回避し、複数回の実機確認でサイドバーが安定して表示されることを確認した。折り返しが必要な説明文には今後もこの方式を使うこと。
- `LabeledContent`は同一Form内（少なくとも同一セクション内）の行でラベル用の配置カラムを共有する。幅無制限の`Slider`など「際限なく広がりたい」コントロールを`LabeledContent`と同じForm内に置くと、その共有カラム計算が壊れ、長いラベルが左端で欠けたり、キャプションの`Text`がウィンドウ幅を無視してそのまま突き抜けたりする（v1.2.3で発見・修正）。幅が可変・無制限になり得る行は`LabeledContent`を使わず、`VStack(alignment: .leading)`でラベルとコントロールを別行に分離し、`.frame(maxWidth:)`で明示的に幅を制限すること。
- `.navigationSplitViewColumnWidth(min:ideal:max:)`はidealではなくmaxに近い値で初期サイズが決まる、かつ状況によりサイドバーの初期表示が不安定になることを確認。
- `MenuBarExtra`の`.window`スタイル配下では`ScrollView`はコンテンツの理想サイズを正しく報告できず、ポップアップが数px程度に潰れることがある（v1.2.1で実際に発生、v1.2.2で素の`VStack`に戻して解消）。同様の構成を作る際は要注意。一方、`NavigationSplitView`のdetailペイン（`Settings`ウィンドウ等）ではこの制約はなく、`Form`が縦方向にスクロールしない場合は素直に`ScrollView`で包んで問題ない（v1.2.3で確認）。
- 上記と同系統の問題として、`.window`スタイルの`MenuBarExtra`は**内容が高頻度で更新されるだけでもステータスバーの表示が不安定になる**（v1.2.4で発見・修正）。`MenuBarView`はメニューを開いている間`refreshStatus()`を1秒間隔で呼ぶが、`@Published`プロパティは値が変化していなくても代入するだけで`objectWillChange`を発火するため、同じ状態でも毎秒SwiftUIへの再描画要求が発生し、ステータスバーの`Label(shortStatus, systemImage: icon)`とポップアップ本体が再描画され続けてアイコンがちらつく・一瞬消えるという形で表面化した。`MenuBarExtra`をバックエンドとする`ObservableObject`を高頻度ポーリングで更新する場合は、`if 現在値 != 新しい値 { 現在値 = 新しい値 }`のように**値が実際に変化したときだけ代入する**こと（`MenuBarViewModel.setIfChanged`参照）。

### Coordinatorのシャットダウンとバックグラウンドジョブ
- ランタイム自動インストール（`AppState.install_missing_runtimes()`）が起動するジョブは、`uv`のサブプロセスを`start_new_session=True`で独立したプロセスグループとして起動する（`RuntimeUpdater._command`）。これは明示的なジョブキャンセル（`/cancel`エンドポイント）時に`killpg`で確実に止められるようにするための設計。
- ただし、v1.2.2以前はCoordinatorの終了処理（`main.py`の`serve()`の`finally`節）が実行中のジョブを一切キャンセルしていなかったため、SIGTERM/SIGKILLでCoordinatorが終了しても、インストール中の`uv`プロセスは孤児化してバックグラウンドで動き続けていた（グローバル共有キャッシュに書いていたv1.2.2以前は実害が目立たなかった）。
- v1.2.3で`JobManager.cancel_all()`を追加し、`serve()`の`finally`節で最初に呼ぶよう修正。複数ジョブは`asyncio.gather`で並行キャンセルすること（逐次だと5秒×ジョブ数の待ち時間になり、テストのタイムアウトと競合して不安定になることを結合テストで確認済み）。

### ad-hoc署名とlaunchdラベル競合（v1.3.0で対策・完全解決ではない）
- このアプリはDeveloper IDを設定しない限りad-hoc署名（`codesign --sign -`）でビルドされる（`scripts/build-release.sh`）。ad-hocの署名IDはビルドごとに変わる。
- `com.yukiorita.MLXBar.Coordinator`というlaunchdラベルを、`SMAppService.agent(...)`経由の登録と、手動フォールバックplist（`~/Library/LaunchAgents/`）の**2つの経路が共有**している。過去にインストールした別ビルド（署名IDが異なる）の登録が、Finderでの上書きインストールなど「すべてのデータを削除して終了」を経由しない入れ替えで残っていると、新しいインストールの起動時にOSがLaunch Constraint Violationとして`SIGKILL (Code Signature Invalid)`を出すことがある（v1.2.4リリース検証中に実機で発生・確認）。
- v1.3.0で`CoordinatorClient.startService()`に、初回の健全性チェックが失敗した場合の防御的な回復ステップ（両登録経路を`launchctl bootout`してから再登録・再試行）を追加した。**ただしこれはmacOS内部の挙動に基づく推測であり、完全な解決策として証明されたものではない**（OS側のLaunch Constraint機構の詳細はソースからは検証不能）。同種の不具合を踏んだ場合は、まず`launchctl bootout gui/$(id -u)/com.yukiorita.MLXBar.Coordinator`と`~/Library/LaunchAgents/com.yukiorita.MLXBar.Coordinator.plist`の削除を試すこと。
- 開発中に複数のビルド（debugバイナリ、`dist/MLXBar.app`、`/Applications/MLXBar.app`等）を同じMacで行き来してテストすると、この競合を自分で引き起こしやすい。テスト後は`mlxbarctl remove-all-data --yes`または上記の手動bootoutでクリーンな状態に戻すこと。

### Coordinator側の`__version__`が2バージョン以上古いまま放置されていた（v1.3.0で発見・修正）
- `Coordinator/mlxbar/__init__.py`の`__version__`が`"1.1.0"`のまま、v1.2.0〜v1.2.4のリリースを通じて一度も更新されていなかった。`/api/v1/health`はこの値を返す。
- `CoordinatorClient.isExpectedVersionHealthy()`（Swift側）は、`/api/v1/health`が返す`version`とアプリ本体の`CFBundleShortVersionString`が**完全一致**することを健全性の条件にしている。バージョンが食い違ったままだと、この一致チェックは**恒久的に失敗し続ける**。
- 実害: `startService()`は「バージョン一致した健全な応答」が得られるまで`installFallbackLaunchAgent()`（bootout→bootstrap→kickstart）を経由し続ける設計のため、Coordinator自体は正常に動いていても、**アプリを起動するたびに毎回LaunchAgentの登録を丸ごと作り直していた**可能性が高い。GUIの「サービス稼働中」表示（`refreshStatus()`が直接叩く`/api/v1/status`）はこのチェックを経由しないため気付きにくい。
- v1.3.0で発見した本件の教訓を踏まえると、A2で対策した「Launch Constraint Violation」自体も、実は**この不要な毎回の再登録サイクルが遠因だった可能性がある**（登録を作り直す回数が多いほど、ad-hoc署名のIDが変わるビルドの入れ替わりと衝突する機会も増える）。今後、同種の不可解な起動不安定を調査する際は、まず`curl --unix-socket .../coordinator.sock http://mlxbar/api/v1/health`で返る`version`とアプリの実バージョンが一致しているか確認すること。

### Coordinatorのクラッシュログ（v1.3.0で追加）
- launchdの設定ファイルはセキュリティ上の理由（共有`/tmp`のシンボリックリンク攻撃を避けるため）で`StandardErrorPath`をあえて設定していない。そのため、`logging`モジュールが初期化される前の例外や、どこにも捕捉されない例外は、以前は完全に失われていた。
- `main.py`の`run()`に、`logging`の状態に依存しない緊急ログ書き込み（`_emergency_log`）を追加。書き込み先は`~/Library/Application Support/MLXBar/logs/coordinator-crash.log`。SIGKILLはOSレベルで捕捉不可能なため対象外（これは物理的な制約であり、コードでは解決できない）。

### `POST /api/v1/system/reset`のシャットダウン順序（v1.3.0で追加）
- 「すべてのデータを削除」の実体（設定・トークン・DB・ランタイム・uvキャッシュ・workerソケットの削除）はCoordinator自身が持つ`AppState.reset_all()`に一本化した（`state.py`）。以前はSwift側がハードコードしたパスリストで削除しており、`MLXBAR_HOME`環境変数でrootを変えた場合に理論上ずれる余地があった。
- 実装上、**レスポンスを返すより前にプロセスを終了させてはいけない**。`reset_all()`は同期的に（ジョブキャンセル→DB接続クローズ→ログハンドラクローズ→`control/coordinator.sock`以外の削除）まで行った後、`state.management_server.should_exit = True`をセットするだけでリクエストハンドラを返す。実際のシャットダウン処理（`serve()`の`finally`節）は、uvicornがこのHTTPレスポンスを書き終えた後の次のイベントループサイクルで走る。**`os.kill(os.getpid(), signal.SIGTERM)`による自己シグナル送信は、リクエストハンドラ自身が実行中のイベントループに対して確実に非同期処理されるとは限らず、実機テストで`finally`節が実行されないまま謎のプロセス終了が起きることを確認した**ため採用していない。`management`（uvicornの`Server`インスタンス）を`AppState`に保持しておき、`should_exit`を直接立てる方式に変更した。
- ソケットファイル（`control/coordinator.sock`）は`reset_all()`では触らず、既存の`serve()`の`finally`節の`socket_path.unlink(missing_ok=True)`に完全に委譲する。理由: レスポンスを返す時点でリスナーがまだこのソケットを握っているため。
- **この機能を実装中に、無関係な既存の起動レースを発見・修正した**: `state.install_missing_runtimes()`（自動ランタイムインストールのジョブ登録）が、管理APIがリクエストを受け付け可能になった**後**に呼ばれていたため、起動直後の数十〜百ミリ秒の間に外部からリクエスト（特に`/system/reset`のようにDBを閉じる処理）が届くと、起動処理自体が「閉じたDBへの書き込み」でクラッシュすることがあった（結合テストで実際に再現・確認）。`install_missing_runtimes()`とその後のスキャン判定を、管理APIサーバー（`management`のuvicorn起動）より前に移動して解決した。

### CLIとGUIの機能パリティにおけるOS API境界（v1.3.0で対応）
- `mlxbarctl`（Python）はGUIのほぼ全機能を操作できるようにしたが、2点だけmacOSのAPI境界により完全な等価にはできない:
  - **ログイン時に起動**: `SMAppService`はSwift/ObjC専用のFoundation APIで、Pythonから直接呼べない。CLIは`general.launchAtLogin`という設定値だけを変更でき、実際のOS登録はGUIアプリが起動・設定再取得したタイミングで`MenuBarViewModel.reconcileLaunchAtLogin()`が反映する（次にGUIが起動するまでは希望状態と実際の登録がずれる）。
  - **`remove-all-data`のログイン項目解除**: 同じ理由で`SMAppService.unregister()`をCLIから呼べない。`launchctl bootout`（Pythonの`subprocess`から実行可能）でサービス自体は確実に停止するため実害はなく、システム設定のログイン項目に見た目上のエントリが残るだけ（アプリ本体を削除すれば消える）。
- 逆に言うと、`launchctl`・`defaults`はただのコマンドラインツールなのでPythonからも`subprocess`で問題なく呼べる。SwiftでしかできないのはFoundationの`ServiceManagement`フレームワーク呼び出しだけ、という切り分けを覚えておくこと。

### メニューバーを開くと即座にクラッシュしていた問題（`Bundle.module`の資材解決ミスマッチ、v1.1.0から潜在・v1.3.0で発見）
- 症状: メニューバーのアイコンをクリックすると、ポップアップが開く前に一瞬で消える（プロセスごと終了する）。v1.2.4の「アイコンがフラッシュして消える」報告と見た目は同じだが**原因は別**（v1.2.4は`refreshStatus()`の毎秒再描画によるちらつき、こちらは`fatalError`によるプロセス終了）。
- 根本原因: [Localization.swift](Sources/MLXBar/Services/Localization.swift)の`AppLanguage.bundle(for:)`が、SwiftPM自動生成の`Bundle.module`アクセサ経由でローカライズ資材（`MLXBar_MLXBar.bundle`）を読んでいた。このアクセサ（executable target向け生成コード）は`Bundle.main.bundleURL`（`.app`直下）を探すが、`scripts/build-release.sh`は同バンドルを`Contents/Resources/`配下に配置している（[CoordinatorClient.swift:329](Sources/MLXBar/Services/CoordinatorClient.swift:329)の`bundleResources`もこちらの規約に従う）。探索先と実配置がずれており、`resource_bundle_accessor.swift`生成コードの`fatalError("could not load resource bundle: ...")`でプロセスごと終了していた。
- 発火条件: `AppLanguage.text(_:language:)`は`current == sourceLanguage`（`"ja"`）のとき`Bundle.module`に触れず即returnするガードがある。`guiLanguage`はCoordinatorの`general.language`設定が無ければ`"en"`がデフォルト（[MenuBarViewModel.swift:502](Sources/MLXBar/MenuBar/MenuBarViewModel.swift:502)）のため、**新規インストール・英語UIのユーザーは高確率で即クラッシュする**一方、GUI言語を日本語で使うことが多い開発環境では踏みにくく、実機検証をすり抜けていたと考えられる。`AppLanguage`・`guiLanguage`はどちらもv1.1.0で導入されており、この構造自体はそこから潜在していた可能性が高い。
- 修正: `AppLanguage`に`resourceBundle`という static let を追加し、`CoordinatorClient`と同じ`Contents/Resources/MLXBar_MLXBar.bundle`パスを最初に試し、見つからない場合（`swift run`でのローカル実行など`.app`化されていない場合）のみ`Bundle.module`にフォールバックするようにした。
- 教訓: 同一コードベース内でリソースバンドルの場所を解決するロジックが2系統（SwiftPMの`Bundle.module`規約 と、`CoordinatorClient`が使う手動`Contents/Resources`パス構築）併存していたのが根本原因。**今後`Bundle.module`を新規に使う箇所を追加する前に、`CoordinatorClient.swift`の`bundleResources`と同じ手動解決に統一できないか検討すること。**

### mlx-vlmモデルでの生成が`apply_chat_template requires jinja2`で失敗していた問題（v1.3.2で発見・修正）
- 症状: ZCode（外部のOpenAI互換クライアント）からmlx-vlmモデル（画像対応、モデル名`Laguna-S-2.1-oQ2e`）へ生成リクエストを送ると、`GENERATION_FAILED` / `apply_chat_template requires jinja2 to be installed. Please install it using pip install jinja2.`で失敗する。mlx-lmモデル（テキストのみ）では発生しない。
- 根本原因: `apply_chat_template`は`mlx_vlm.prompt_utils.apply_chat_template`（[Workers/mlx_vlm_worker/adapter.py:43](Workers/mlx_vlm_worker/adapter.py:43)）経由で最終的にHugging Face `transformers`のtokenizer/processorが担当するが、`transformers`自体は`jinja2`を必須依存として宣言していない（オプション扱い）。ランタイムのインストール処理（[Coordinator/mlxbar/runtimes/updater.py](Coordinator/mlxbar/runtimes/updater.py)の`stage()`）は`uv pip install --python <venv> <engine> fastapi uvicorn`のみを指定しており、`jinja2`を明示していなかった。
- 実機の`~/Library/Application Support/MLXBar/runtimes/{mlx-lm,mlx-vlm}/slots/*/requirements.lock`を比較して確定: **`mlx-lm`側はたまたま別の依存経由で`jinja2==3.1.6`が入っていた**（`mlx-vlm`側の依存グラフには`mlx-audio`・`opencv-python`など`mlx-lm`にはない多数のパッケージが入っており、逆に`mlx-lm`側にだけ`jinja2`/`markupsafe`が入っている非対称な状態だった）一方、**`mlx-vlm`側には`jinja2`もその依存の`markupsafe`も一切含まれていなかった**。`.venv`のsite-packagesを直接確認しても`mlx-vlm`スロットにだけ`jinja2`ディレクトリが存在しないことを確認済み。
- 修正: `updater.py`の`uv pip install`コマンドに`jinja2>=3.1,<4`を明示追加。両エンジンとも今後は依存解決の巡り合わせに関係なく確実にインストールされる。
- 注意（重要）: **この修正はインストーラーのコードのみを直しており、既にインストール済みのランタイムスロットは自動的には直らない。** 影響を受けたユーザーは、アップデート後にランタイム画面（または`mlxbarctl runtime`）からmlx-vlmランタイムを再インストール・更新して新しいslotを作らせる必要がある。このMac自体は応急処置として、既存slotの`.venv`へ`uv pip install --python <venv>/bin/python jinja2`を直接実行し、`requirements.lock`も`uv pip freeze`で更新して整合させた上でCoordinatorを`launchctl kickstart -k`で再起動して即時復旧させた。
- 教訓: `uv pip install`で「そのパッケージが動くのに必要な全依存」を、宣言されたものだけに頼って揃えようとすると、あるライブラリ（ここでは`transformers`）がオプション扱いにしている実行時必須の機能（`apply_chat_template`のjinja2レンダリング）が、たまたま別経路で入るかどうかに左右されてしまう。**両エンジンに共通で必要な「暗黙の実行時依存」がないか、他のアダプター（[Workers/common](Workers/common)）の使い方も含めて棚卸しする価値がある。**

## リリース時のバージョン文字列更新チェックリスト

新バージョンをリリースする際、以下のファイルの版数表記をすべて更新すること（漏れやすい）:

- `Coordinator/pyproject.toml`（`version`）
- `Coordinator/mlxbar/__init__.py`（`__version__`） — ★ `/api/v1/health`が返す値。**v1.1.0からv1.3.0まで更新漏れしていたことをv1.3.0で発見**（下記参照）。今後は特に注意。
- `Packaging/Info.plist`（`CFBundleShortVersionString`・`CFBundleVersion`）
- `scripts/build-release.sh`（`VERSION=`）
- `scripts/verify-release.sh`（デフォルトの`VERSION`フォールバック値）
- `README.md`（先頭のVersion表記、DMGファイル名2箇所）
- `CHANGELOG.md`（新バージョンの節を先頭に追加）
- `RELEASE_NOTES_v{version}.md`（新規作成、SHA-256は実ビルド後に追記）
- `TEST_PLAN_v{version}.md`（新規作成）
- `DESIGN_v{version}.md`（新規作成。**v1.6.2以降、`scripts/build-release.sh`のDMGステージングが`DESIGN_v$VERSION.md`を必須で参照するため、無いと`set -e`でビルドが止まる。** バグ修正のみのリリースでも短いもので良いので必ず置く）
- **紹介サイトはこのリポジトリに無い。** `oriyu90/studio-rizi`の`website/projects/mlx-bar/`で更新する（`softwareVersion`のJSON-LD、kicker、動作環境表の「最新版」、手順のDMG名、`content.js`のリリース版数と日付）。**4言語ぶんあるので箇所数が多い。正確な一覧は非公開メモ側にある。** 共通ルール「htmlの更新は実装後の最終作業」に従い、DMGを公開してから行うこと。

## ウェブサイトの多言語対応について

- `website/index.html`は日本語・English・中文・Português の4言語に対応（2026-08-19対応、common rulesルール3準拠）。
- 実装はクライアントサイドJS（`window.MLXBarI18n`）による切り替え方式。ブラウザ言語を自動判定し、`localStorage`に保存した選択を優先する。
- HTML本文中の`data-i18n="key"`が翻訳対象の目印。`<script>`内の`I18N`オブジェクト（`ja`/`en`/`zh`/`pt`の4キー）に翻訳文を保持している。
- **本文コピーを変更する際は、raw HTML（日本語）と`I18N.ja`/`I18N.en`/`I18N.zh`/`I18N.pt`の計5箇所すべてを揃えて更新すること。** 1箇所でも漏れると、その言語に切り替えたときだけ古い文言が残る。
- `<code>`や`<b>`などのHTMLタグを含む翻訳文には`data-i18n-html="true"`を付け忘れないこと（付け忘れるとタグが文字列としてそのまま表示される）。

## Cloudflare Pages

- プロジェクト名`mlx-bar`はGit連携（GitHub `oriyu90/mlx-bar`、`main`ブランチ）で作成済み。`destination_dir`は`website`、`build_command`は空。設定変更は不要（2026-08-19時点でAPI経由確認済み）。

## ランタイムインストールのuvキャッシュについて

- mlx-lm・mlx-vlmのインストール（`Coordinator/mlxbar/runtimes/updater.py`）は`UV_CACHE_DIR`を`Application Support/MLXBar/uv-cache`に固定している（v1.2.3〜）。「すべてのデータを削除して終了」がMLXBar専用フォルダを丸ごと削除する対象に含めるための変更で、他プロジェクトが使う共有の`~/.cache/uv`は一切触らない。
- v1.2.2以前でインストール済みのユーザーは、共有キャッシュ側に残った古いmlx-lm/mlx-vlmのダウンロード分がそのまま残る。これはMLXBar固有のデータではなく他プロジェクトとも共有される領域のため、アプリ側から追跡・削除する対象にはしない方針。

## その他

- （2026-08-19時点）開発用Macの重複コピー`MLXBar 2.app`は解消済み。今後また似た状態に気付いたら削除を検討。

### v1.4.0永続Prompt Cache（2026-08-21）

- v1.3.7の`PromptCacheState`は、同一Worker内のRAM cacheとして実機で約88倍の改善を確認済みなので置換しない。v1.4.0は`APCManager(num_blocks=0)`と`DiskBlockStore`を下位の永続tierとして追加する。Qwen 27BでAPC RAM block/exact LRUを併用すると同じ大容量状態を二重保持するため、`APC_EXACT_CACHE_ENTRIES=0`に固定する。
- Qwen3.8/Qwen3.5 hybrid cacheは任意blockを連結できずexact snapshot経路になる。既定16-token guardでは最初のユーザー文が変わるとstable system/tools prefixまでhitしにくいため、`APC_EXACT_PREFIX_GUARD_TOKENS=256`を使用する。この末尾だけは再計算する。
- 永続namespaceはprompt本文から独自判定しない。モデルのresolved path、config/tokenizer/chat template内容、safetensorsの名前/size/mtime、mlx-vlm版、MLXBar cache形式版だけをSHA-256化し、実際のtoken-prefix一致はmlx-vlm APCへ委譲する。
- cache rootは`Application Support/MLXBar/prompt-cache/mlx-vlm`、既定上限5 GB。KV tensorだけでなくtoken IDsもsafetensors metadataへ残るため、root/namespaceは0700とし、設定画面から消去できるようにする。全データ削除はApplication Support rootごと削除するため自動的に包含される。
- APC起因の例外はtracebackが`mlx_vlm/apc`を通るか、APC/safetensors/cache snapshotを示す場合だけfallback対象とする。応答送信前ならAPCを閉じ、PromptCacheStateを再作成して一度再試行する。モデルkernel等の無関係な失敗は二重実行しない。
- 中心シナリオ実測: Qwen3.8-27B、15,852 prompt tokensでcold 56.106秒、Disk APC同一質問1.531秒（15,596 cached）、最初の質問変更1.414秒（15,596 cached）、RAM継続0.262秒（15,862 cached）。モデルload 12.912秒、APC `num_blocks=0` / resident bytes 0 / reject 0。exact snapshot 2境界はclose後約2.37 GBだったため、既定5 GBはこのサイズの巨大prefixを概ね2組保持する容量である。追加stressは`TEST_PLAN_v1.4.0.md`参照。
- FastAPI 0.141.1／Starlette 1.6.0の`TestClient`は`httpx2`を優先するため、開発依存へ`httpx2>=2,<3`を追加した。本体側は`httpx`のAPIを使用するため置換せず、テスト設定で`StarletteDeprecationWarning`をエラーにして将来の退行を検出する。

### v1.4.1生成ロック自己回復（2026-08-21）

- 実機v1.4.0で、Coordinatorは生存、Workerとモデルも正常、メモリ空き95%以上、`activeRequestCount=0`なのに`queuedRequestCount=1`が10分以上増え続け、Worker CPUがidleという停止状態を確認した。直前のZCode SSE要求は応答tokenを返さず終了していた。モデル計算やPrompt Cacheではなく、切断された要求が生成ロックを残したことが直接原因である。
- OpenAI API、管理API、APIアクセス記録middlewareの各SSE wrapperは、外側の接続がキャンセル・終了したとき直下のasync generatorを`aclose()`する。Pythonの暗黙的なgenerator終了順序に依存せず、wrapperが重なっていてもSupervisorの`finally`まで終了を伝播させる。
- 生成ロックに要求IDのownerを持たせる。queue登録はロック取得とowner設定が完了するまで残し、正常なqueue→active handoffを孤立と誤認しない。releaseは同じownerだけが実行でき、遅れて走った古い要求の`finally`が新しい要求のロックを解放することを防ぐ。
- 自己回復条件は「lockがlocked」「ownerがactiveにもqueuedにも存在しない」「active要求が0」の論理積に限定する。新規要求、queue heartbeat、状態取得、idle待機で評価し、該当時だけロックを解放する。`active`があるのにlockがない状態は`inconsistent`として報告するが、並列生成を招く自動取得はしない。
- 管理状態へ`generationLockState`と`generationLockRecoveries`を追加する。本文、tool定義、APIキー、要求IDは診断応答へ出さない。
- 障害注入、正常handoff、別owner release拒否、待機済み要求の回復、重なったSSE wrapperのclose、100並列の約3分の1を切断するstressを含むPythonテスト171件を通過した。詳細は`TEST_PLAN_v1.4.1.md`を参照。
