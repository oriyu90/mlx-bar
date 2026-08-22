# MLXBar v1.6.0 実装プラン — セッション中断後の立ち上がり

対象: 中断（キャンセル・切断）後に 10万トークンを cold prefill し直す問題。
実測 (v1.5.3 / Qwen3.8-27B-MLX-8bit / ZCode): memory hit 2秒 → cold 477秒。

## 設計の不変条件（全項目が従う）

1. バージョン番号・モデル名・アーキ名で分岐しない。**能力の有無**で分岐する。
2. 数値定数はモデル `config.json` から算出する。固定 GB / 固定トークン数を書かない。
3. キャッシュの失敗は必ず cold へ degrade する。ただし **degrade した理由を記録する**。
4. 永続化するのは安定境界（プロンプト末尾）だけ。生成途中の状態を書かない。
5. ランタイム内部の構造に触るときは必ず能力プローブと事後検証を通す。

## 依拠するランタイム事実（確認済み・mlx-vlm 0.6.15）

| 事実 | 位置 | 使う項目 |
|---|---|---|
| `GenerationResult.token: Optional[int]` | `generate/common.py:207` | A-2 |
| `ArraysCache.state` に getter/setter | `models/cache.py:638` | A-3 / B-5 |
| `find_prefix_length()` は `.cache` 読み出しより前 | `generate/dispatch.py:845-846` | B-1 / B-5 |
| `update(all_ids, tracked_cache)` は完走時のみ | `generate/dispatch.py:1062` | A-1 |
| `all_ids = full_input_ids_list + generated_tokens` | `generate/dispatch.py:1063` | A-1 |
| `make_prompt_cache(model)` | `models/cache.py:45` | A-3 復元 |

いずれも `getattr` / `hasattr` で存在確認してから使い、欠けていれば最適化なしで継続する。

---

## A. 中断・キャンセル

### A-1 中断時にキャッシュを破棄せず「確定」する
`mlx_vlm_worker/adapter.py` の `finally` で `_reset_prompt_cache()` していたのを、
`token_ids = 取得済みプロンプトIDs + 生成済みIDs` に再構成して cache を保持する方式へ変更。
dispatch が完走時に書くのと**同一の値**を再現する。

**事後検証（必須）**: 保持してよいのは「その cache が実際に今回の生成で進んだ」ときだけ。
`max(getattr(c, "offset", 0))` が `len(prompt_ids) + generated` と一致することを確認し、
一致しなければ従来どおり破棄する。cold 経路（dispatch が別の cache を作った場合）は
prompt_cache_state.cache が前ターンのままなので、この検証で自動的に弾かれる。

### A-2 生成トークン ID の逐次記録
`response.token` を `hasattr` で拾って蓄積。取れないランタイムでは A-1 を無効化し、
理由 `token_ids_unavailable` を記録して従来の破棄へ落とす。**出力テキストの再エンコードはしない**
（トークナイザは往復同一性を保証しない）。

### A-3 分岐用チェックポイント（trim 不可アーキ向け）
prefill 完了時点（＝最初のトークン）で cache を複製し、「プロンプト末尾」の状態として保持。
中断後に部分出力を捨てたクライアントが再送しても、この地点へ復元して再利用できる。

- 複製は `.state` ツリーの深いコピー（`mx.array` を新規バッファへ）。`trim` に依存しない。
- **rollback 可能なアーキでは取らない**（trim で足りるため）。
- 予算ガード: C-1 の算出値と `memory_pressure_reason()` を両方通す。
- 設定 `promptCache.branchCheckpoint`: `auto`(既定) / `off`。

### A-4 メモリ逼迫による中断は「破棄」を維持する
A-1 の保持対象は利用者起因のキャンセルと切断のみ。`MEMORY_PRESSURE` は
メモリを空けるための中断なので保持は逆効果。理由 `memory_pressure` を記録して破棄する。
（当初案では A-1 と同一扱いとしていたが、目的が逆であるため分離する。）

### A-5 キャンセル契約をテストで固定する
「キャンセルは warm を保つ / メモリ逼迫は捨てる / 検証失敗時は捨てる」を回帰テスト化。

---

## B. キャッシュ階層

### B-1 巻き戻し不能な再利用を「事前に」拒否する
`PromptCacheState` を継承した `GuardedPromptCacheState` を導入し、`find_prefix_length()` で
- 巻き戻しが必要（`prefix_len < 保持長`）かつ
- cache 配列に `trim` を持たない要素がある
場合に **0 を返す**。dispatch 側の `0 < prefix_len` 条件が偽になり、trim 呼び出しへ到達しない。

これにより `'ArraysCache' object has no attribute 'trim'` の例外自体が発生しなくなる。
v1.5.1/v1.5.2 の例外検出（`_is_cache_reuse_failure`）は安全網として残す。

**壊れない理由**: 判定材料は `hasattr(c, "trim")` と `is_trimmable()` の**存在**のみ。
上流が修正されれば `trim` が生えるので自動的に通常経路へ戻る。

### B-2 APC メモリブロックを設定可能にする
`APCManager(num_blocks=0)` 固定をやめ、`promptCache.memoryBlocks` を追加。
`off`(既定, =0, 現行の実証済み挙動) / `auto`(C-1 の予算から算出)。
**既定は off のまま**とする。ブロック経路の挙動は実機 27B での計測が未実施であり、
既定を変えるだけの証拠がないため。保守メモに計測手順とともに記録する。

### B-3 共有モジュールへ切り出す
`Workers/common/cache_state.py`（新規）に以下を集約し、両ワーカーが使う。
- 能力プローブ（trim 可否 / state 可否 / token 取得可否）
- 予算算出（C-1）
- `.state` ツリーの複製・復元・safetensors 直列化
- tier 名と degrade 理由の語彙

mlx-lm 側の既存 `prompt_cache.py`（動作実績あり）は**壊さない**。共有できる計算だけを移す。

### B-4 永続化は安定 prefix に限定し、guard を実測から決める
mlx-lm 側は `prompt_length` で頭打ち済み（保守メモ記載の教訓）。mlx-vlm 側は APC の
`exact_cache_guard_tokens` が固定 256 で、ZCode のように 1 ターンで数千トークン伸びる
クライアントでは次ターンと一致しない。直近ターンのプロンプト長の増分から算出して
`manager.exact_cache_guard_tokens` へ反映する（属性が無ければ何もしない）。

### B-5 mlx-vlm に「最長利用可能 prefix」のディスク層を持たせる
A-3 のチェックポイントを safetensors + `index.json` で永続化し、**長い順に走査して
digest 一致**を探す（mlx-lm 側と同じ方式）。復元は `find_prefix_length()` の中で
`.cache` / `.token_ids` を差し替える形で行う。

**安全策**: `.cache` を property 化し、「今回の要求で `find_prefix_length()` が返した長さ」と
整合する cache だけを返す。dispatch が将来 `.cache` を先に読む実装へ変わっても、
返る prefix 長と cache が食い違わない。

---

## C. 予算をモデルから算出する

### C-1 bytes/token の算出
```
per_token = Σ(full_attention 層) 2 × kv_heads × head_dim × dtype_bytes
fixed     = Σ(linear/recurrent 層) の固定状態バイト数
snapshot(N) = fixed + per_token × N
```
`text_config.layer_types` / `num_key_value_heads` / `head_dim` から読む。
読めない場合は「不明」とし、**推測値で判断しない**（無効化ではなく従来動作を維持）。

### C-2 予算不足をロード時に告知する
「このモデルは N トークンで X GB 必要（現在の上限 Y GB）」を capabilities/status に載せ、
GUI とログへ出す。

### C-3 1 世代も入らないならディスク層を自動無効化する
`disabledReason = "budget_insufficient"` にして書き込みを止める。
現状は上限超過のスナップショットを書いては捨てており、SSD 書き込みだけが残る。

### C-4 メモリ上限比の算出根拠を状態に出す
`wiredLimitRatio` / `cacheLimitRatio` の適用結果と、重み実サイズ＋想定 KV の合計を
`memory_stats()` に含める。**既定値は変えない**（実機検証なしに安全側を動かさない）。

### C-5 書き込み量の上限
`promptCache.diskWriteBudgetGB`（1 セッションあたり、既定 32）を追加。
超えたら `disabledReason = "write_budget_reached"` で以後書かない。

---

## D. ライフサイクル

### D-1 再起動後に前回のモデルを先行ロードする
`last_loaded_model_id` は既に保存されている。起動時に
`general.preloadLastModel`（既定 true）でバックグラウンドロードする。
**起動処理の完了を待たせない**（`install_missing_runtimes()` の起動レースの教訓に従い、
管理 API を先に上げてからジョブとして投入する）。

### D-2 ロード中のランタイム切替を禁止する
モデルがロードされている間はスロット切替を行わない不変条件を明文化・テスト化。
（`_raise_if_generations_in_flight` は生成中のみを見ているため、ロード済み判定を追加。）

### D-3 モデル別 warm 保持
今回は**実装しない**。単一ワーカー・単一モデル前提の supervisor 構造を変える必要があり、
v1.6.0 の目的（中断復帰）に対して費用対効果が低い。保守メモへ理由とともに記録する。

### D-4 ロード中は 409 ではなく待たせる
`autoLoadOnAPIRequest` が真のとき、ロード進行中の要求は `MODEL_NOT_LOADED` で即時失敗させず、
既存のロードの完了を待って続行する（`model_autoload_lock` の保持時間内に収める）。

---

## E. 観測性

- **E-1** `cache_tier` をメニューバー／状態 API に出す。cold が 2 回連続したら警告。
- **E-2** `reuseFailures` / `coldReason` を GUI に日本語で出す。
- **E-3** 再利用可否と予算をロード完了時に表示する（事後ログではなく事前告知）。
- **E-4** first token までの見積り時間（`prompt_tokens / prompt_tps`）を進捗イベントに載せる。
- **E-5** `cache_tier: cold` に理由を添える:
  `no_prefix` / `reuse_unsupported` / `budget_insufficient` / `cancelled_previous` /
  `runtime_changed` / `memory_pressure` / `token_ids_unavailable` / `write_budget_reached`。
  SQLite に `cold_reason` 列を追加（既存 DB は自動 migration）。

---

## F. クライアント契約

- **F-1** 直前要求との共通 prefix 長と**分岐位置**を metrics/API ログへ出す。
  system prompt の時刻混入や tool 順序の非決定性を即座に発見できる。本文は保存しない。
- **F-2** `/v1/models` に `prefix_reuse` と `recommended_max_prompt_tokens` を追加。
- **F-3** 中断再開プロトコルを README/リリースノートに明記する
  （部分出力を履歴に残して再送すれば prefill ゼロで再開できる）。
- **F-4** `maxPromptCharacters` が実測 968,524 文字の要求を通していた件を調査し、
  予算判定（C-2）と接続する。

---

## G. 維持する仕組み

- **G-1** 中断→再開の E2E 回帰テスト。`cache_tier` と degrade 理由を固定する。
  偽モジュールではなく**実ファイルパスを持つ疑似ランタイム**で検証する
  （v1.5.1 の取りこぼしの教訓）。
- **G-2** `scripts/cache-capability-matrix.py`: カタログ内の全モデルについて
  config から能力と予算を算出して一覧化する（重みはロードしない）。
- **G-3** ランタイム更新の activate 時に能力プローブを再取得し、履歴へ残す。
  上流が `trim` を直したら自動的に通常経路へ戻る。

---

## 実装順序

1. `Workers/common/cache_state.py`（B-3 の土台。C-1 の算出、能力プローブ、state 複製）
2. mlx-vlm adapter: B-1 → A-1/A-2 → A-4 → A-3 → B-5 → B-4 → C-2/C-3/C-5 → E-5/F-1
3. mlx-lm adapter: 共有モジュールへの寄せと理由語彙の統一
4. `common/server.py`: 中断種別の伝達（A-4）、ETA（E-4）
5. Coordinator: settings / supervisor env / DB 列 / openai_compat / management / D-1 / D-4 / F-2
6. Swift: E-1 / E-2 / E-3
7. テスト（A-5 / G-1）とスクリプト（G-2）
8. ドキュメントとリリース

## 実装しないと決めたもの（理由つき）

- **D-3 モデル別 warm 保持** — supervisor の単一モデル前提を崩す必要があり、目的に対して割に合わない。
- **B-2 の既定値変更** — 実機 27B での計測なしに既定を動かさない。設定と算出だけ用意する。
- **C-4 の既定値変更** — 同上。根拠の可視化のみ行う。
