# MLXBar v1.6.1 テスト計画

対象: v1.6.0の再利用判定が実機で一度も有効にならなかった原因の修正と、その事実を見えなくしていた可観測性の穴。

## 1. 自動テスト（CIで毎回）

```sh
Coordinator/.venv/bin/python -m pytest Tests -q     # 274件
swift build --disable-sandbox -c release
```

v1.6.1で追加した契約（20件）:

| 契約 | テスト |
|---|---|
| 空の`KVCache`も捕獲可能と判定する | `test_an_empty_kv_cache_is_still_capturable` |
| setterのない`state`は捕獲不可と判定する | `test_a_type_without_a_state_setter_is_not_capturable` |
| キャッシュがラベルより先行する継続を拒否する | `..._refused_when_the_cache_sits_ahead_of_its_labels` |
| ラベルと一致する継続は許可する | `..._allowed_when_the_cache_agrees_with_its_labels` |
| どの形の要求でもランタイムに破棄を残さない | `test_no_answer_ever_leaves_the_runtime_with_tokens_to_drop` |
| 先に`.cache`を読んだ呼び出し側にも復元が届く | `test_a_snapshot_reaches_a_caller_that_read_the_cache_first` |
| 拒否した枝はキャッシュを空にするがNoneにしない | `..._releases_the_cache_but_leaves_it_iterable` |
| 空にしたキャッシュをprefixとして主張しない | `test_an_emptied_cache_is_never_claimed_as_a_prefix` |
| usageが再利用量を報告する（非ストリーム／ストリーム） | `test_usage_reports_the_reused_prefix_...` / `test_streaming_usage_...` |
| 未計測のときはフィールドごと省く | `test_usage_omits_the_reuse_field_when_the_runtime_does_not_measure_it` |
| 報告される再利用量はprompt長を超えない | `test_a_reported_reuse_can_never_exceed_the_prompt_it_belongs_to` |
| Workerのキャッシュ報告がAPIログへ届く | `test_the_workers_cache_report_reaches_the_api_log` |
| 読めなかったカタログ行を公開一覧へ出さない | `test_the_model_list_omits_rows_the_scanner_could_not_read` |
| 公開一覧が入力モダリティを申告する | `test_the_model_list_states_which_inputs_a_model_accepts` |
| 混雑中でも未知のモデル名は404 | `..._is_missing_rather_than_busy_while_another_request_runs` |
| 管理APIの想定外エラーが記録される | `test_an_unexpected_management_failure_is_logged_and_named` |
| 生成中でもキャッシュ状況を答える | `test_the_cache_report_answers_while_the_worker_is_generating` |
| 更新直後のポートは再確保でき、実使用中は失敗する | `test_the_api_port_is_reusable_after_a_restart_but_not_while_taken` |
| スナップショットが索引どおりの名前で保存される | `test_a_persisted_snapshot_lands_under_the_name_the_index_records` |
| 未完了の書き込みを次のロードで回収する | `..._forgets_the_leftovers_of_a_write_that_never_finished` |
| 世代掃除がチェックポイントを消さない | `test_the_namespace_sweep_leaves_the_checkpoint_store_alone` |

## 2. 実ランタイムに対する検証（重みなし・毎リリース必須）

**v1.6.0の2件の不具合は、どちらも偽`mlx.core`では出ない種類だった。** 偽のキャッシュクラスは空の状態でも`state`を返し、テスト内の偽dispatchは`.cache`をnull検査で読まない。実際の`KVCache`は空のとき`state`が例外になり、実際のdispatchは`.cache`を先に読む。**この差が「37件のテストが通るのに実機では一度も有効にならない」を作った。**

```sh
VENV="$HOME/Library/Application Support/MLXBar/runtimes/mlx-vlm/slots/<slot>/.venv/bin/python"
"$VENV" scripts/verify-prompt-cache-runtime.py
```

モデルの重みは不要（`KVCache` / `ArraysCache` / `PromptCacheState`は単体で生成できる）。

実施結果（mlx-vlm 0.6.15、v1.6.0との比較）:

| 確認 | v1.6.0 | v1.6.1 |
|---|---|---|
| `can_capture(空のハイブリッド)` | **False** | True |
| `rollback_capability(空のハイブリッド)` | **`none`** | `checkpoint` |
| `rollback_capability(KVCacheのみ)` | `trim` | `trim` |
| 継続・キャッシュがラベルと一致 | 破棄なし | 破棄なし |
| 継続・キャッシュがラベルより先行 | **`trim()`到達** | 破棄なし |
| 枝分かれ（先行の有無を問わず） | 破棄なし | 破棄なし |
| 先に`.cache`を読んだ呼び出し側への復元 | **届かない** | 届く |
| 拒否した枝の後の破棄量計算 | **`TypeError`** | 0 |
| 実配列でのcapture/restore往復 | 一致 | 一致 |

`TypeError`はv1.6.0では表面化しない（復元経路自体に到達しないため）が、順序ガードだけを直すと露出する。空リストを残す設計はこの両方を同時に閉じている。

## 3. 実機（27 GBの重みが必要）

ロード中モデル: `Qwen3.8-27B-MLX-8bit`（mlx-vlm、16全注意層＋48再帰層、65,536 B/token）。

### 3-1. 能力判定が有効になること

```sh
curl -s --unix-socket "$HOME/Library/Application Support/MLXBar/control/coordinator.sock" \
  http://mlxbar/api/v1/status | python3 -m json.tool | grep -E "rollbackCapability|affordableTokens"
```

| 確認 | 期待 |
|---|---|
| `rollbackCapability` | `checkpoint`（v1.6.0は`none`） |
| `promptCacheHealth.affordableTokens` | `diskMaxGB`から算出した値（12 GBなら約193,000）。v1.6.0は`0` |
| `GET /v1/models`の`prefix_reuse` | `checkpoint` |
| `GET /v1/models`の`recommended_max_prompt_tokens` | 上と同じ値が出ること |

### 3-2. 枝分かれしたターンが再利用されること

同じsystem/user のあと、**別の続き**を2通り送る。

| 確認 | 期待 |
|---|---|
| 1本目のターン | `cache_tier: cold`（初回） |
| 2本目（直線の継続） | `cache_tier: memory` |
| 3本目（1本目から分岐） | `cache_tier: memory`または`disk`。v1.6.0は`cold` |
| `worker-mlx-vlm.log` | `'ArraysCache' object has no attribute 'trim'`が**増えないこと** |
| `GET /api/v1/prompt-cache`の`reuseFailures` | 増えないこと |

### 3-3. 可観測性

| 確認 | 期待 |
|---|---|
| coldのAPIログ行 | `cold_reason`が入る（v1.6.0は常にNULL） |
| 任意のAPIログ行 | `shared_prefix_tokens` / `held_prefix_tokens`が実値（v1.6.0は常に0） |
| `GET /api/v1/prompt-cache` | 200を返すこと |
| 管理APIで例外を起こしたとき | `coordinator.log`に経路とtracebackが残ること |

### 3-4. 書き込み量とメモリ（**この版で初めて動く経路**）

スナップショットは1件で数GBになる。**有効化した状態での実測なしにリリースしないこと。**

| 確認 | 期待 |
|---|---|
| 20ターン程度の会話後の`disk_writes` | 増分しきい値（前回比1.25倍または+8192 token）どおり、ターン数より十分少ないこと |
| `diskBytes` | `promptCache.diskMaxGB`を超えないこと |
| Workerの`disk_writes`合計 | `diskWriteBudgetGB`（既定32）で頭打ちになること |
| 生成中のピークメモリ | `memoryLimitRatio`（既定0.90）未満に収まること。ライブのキャッシュ＋新旧スナップショットで3倍にならないこと |
| メモリ逼迫を作った状態での完了ターン | スナップショットを取らずに応答を優先すること |

### 3-5. OpenClaw（`openai-completions`）

| 確認 | 期待 |
|---|---|
| ツール24件を載せた1ターン | ツール呼び出し→ツール結果→最終応答まで完走 |
| 2ターン目 | `usage.prompt_tokens_details.cached_tokens`が実値で入り、OpenClaw側の`cacheRead`に出ること |
| `GET /v1/models` | `vae` / `text_encoder` / `transformer`が並ばないこと。`modalities`が入ること |
| `timeoutSeconds`未設定＋run timeout指定 | 120秒で`LLM idle timeout`になること（**MLXBar側では塞げない。READMEの手順どおり設定する**） |
| 実行中に未知のモデル名 | HTTP 404 `MODEL_NOT_FOUND`（v1.6.0は429 `ENGINE_BUSY`） |

### 3-6. 回帰（v1.6.0で入った経路を壊していないこと）

| 確認 | 期待 |
|---|---|
| 生成をキャンセルして部分出力ごと送り直す | 再計算がほぼゼロ |
| 部分出力を捨てて送り直す | 直前の完了ターンまで復元される |
| `MEMORY_PRESSURE`での停止 | キャッシュを解放すること（保持しないこと） |
| ストリーム中のtokens/秒表示 | 3トークン到達後に出ること |
| 認証なし40MB×24並列 | RSSが70MB前後に収まること |
