# MLXBar v2.2.0 設計書（実験的機能: アダプティブ・ハイブリッド・コンテキストメモリ）

更新日: 2026-09-11
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

**目的:** 長い会話履歴を、情報の逐語性・関連性・再利用性に応じて3種類の表現へ
動的に振り分ける実験的なメモリ最適化を追加する。

- **EXACT**: 元トークンをそのまま保持（system / tool / コード / 数値 / パス /
  ハッシュ / 直近ターン）。
- **LATENT**: 自然言語の議論を短い要約ノートへ圧縮（ノートは内容アドレスで
  キャッシュし、同一履歴の再送時は再要約を省略）。任意で mlx-lm Worker が
  さらにソフトトークン（モデル自身の埋め込み層による平均プール）へ圧縮し、
  `input_embeddings` で KV キャッシュへ Hybrid Prefill する。
- **COLD**: 関連性の低いターンを推論から除外。ステートレスなクライアントが
  元メッセージを再送するため、正本は失われない。

狙いは TTFT 短縮・KV キャッシュ使用量削減・コンテキスト長削減・消費電力削減を、
コード・数値・tool 結果などの逐語情報を保ったまま同時に狙うこと。

**非目的:**

- **既定挙動を変えない。** `experimental.adaptiveMemory.enabled = false`（既定）で
  完全に休眠。新パッケージへは一切到達せず、キャッシュディレクトリも作られない。
  設定スキーマは追加のみで `schemaVersion` は 1 のまま。Worker / Coordinator↔Worker
  RPC / 既存プロンプトキャッシュ / モデルプール / OpenAI・Anthropic の wire format は
  無変更。
- **学習済み Writer は同梱しない。** v1 の Writer（`meanpool-v1`）はモデル自身の
  入力埋め込み層で平均プールするだけの決定論的・無学習の実装。品質は主張せず、
  Apple Silicon 上での機構の実測用。だから `softToken` は既定 OFF
  （mlx-bar.md「実測がないなら既定値を動かさない」）。
- **MLXBar 独自の全文 Raw 履歴 DB は作らない。** LATENT から EXACT へ戻す必要が
  生じたら、ソフトトークンを復元するのではなく元テキストを再 prefill する
  （＝通常の EXACT 経路へフォールバック）。
- **`contextCompression` と併用しない。** 両方 `enabled` にしようとすると
  `_validate` が `ValueError` を送出（HTTP 422）。
- リランカー・学習型 Policy・Retriever の意味検索・Hybrid KV の永続化は対象外
  （seam と key 構成だけ用意し、実装は将来版）。

## 2. アーキテクチャ（2層、両方とも既定 OFF）

### 層1: コーディネータ（常に安全 / `Coordinator/mlxbar/api/adaptive_memory/`）

`context_compression.py` と同じ「ベストエフォート・例外を投げない・少しでも疑わしければ
入力をそのまま返す」契約。`messages` リストを書き換えるだけで、生成経路には触れない。

| モジュール | 役割 |
|---|---|
| `segment.py` | Context Structure Analyzer。メッセージ単位の逐語リスク判定（コードフェンス、diff/patch、shell、JSON/XML、ファイルパス、ハッシュ/UUID、数値密度 ≥ 0.12、tool traffic、画像、system/developer）。純関数。 |
| `policy.py` | Adaptive Policy v1（決定論的）。`label_messages(middle, policy, verbatim_protection) -> [exact|latent|cold]`。プリセット `fidelity` / `balanced` / `memorySaver` が「LATENT を何ターン残して古いものを COLD にするか」「短い議論ターンをノートにする価値があるか」の2ダイヤルを動かす。`POLICY_VERSION = "am1"`。 |
| `planner.py` | Adaptive Memory Planner。`context_compression` の `_split_point`（tail 境界）/ `_chars`（サイズ）/ `_summarize`（ノート生成）/ `_effective_max_prompt_characters`（発火閾値）を再利用。COLD をドロップ、LATENT の連続ランを1個の合成 `system` ノート（`[Earlier discussion condensed]` 前置）へ、EXACT と tail はそのまま。書き換え後が元の 95% 未満に縮まなければ元を返す。 |
| `latent_store.py` | 内容アドレスのノートキャッシュ。`SHA256(canonical message + role + model fingerprint + POLICY_VERSION)` → ノート。`~/Library/Application Support/MLXBar/adaptive-memory/notes/*.json` ＋ RAM。破損・truncate は miss。件数/日数で prune。 |
| `metrics.py` | `AdaptiveMetrics`（raw/effective chars、exact/latent/cold 数、compression_ratio、policy_ms/writer_ms、cache hit/miss、fallback_reason）。 |

エントリ `maybe_plan_adaptive_memory(workers, loaded, messages, tools, settings, request_id)
-> (messages, summary|None)`。`softToken` 用のノブは `adaptive_memory_worker_options(settings)`
が別途 dict で返し、ルータが生成 `options` へマージ。

### 層2: mlx-lm Worker（ソフトトークン / `Workers/mlx_lm_worker/adaptive_memory/`）

`experimental.adaptiveMemory.softToken` が ON かつ Reader が対応する場合のみ。
**すべて fail-closed**：`prepare()` は `FallbackToExact` 以外を投げない。

| モジュール | 役割 |
|---|---|
| `policy.py` | レンダー済みトークン列の位置バンド分割（head / band / tail）。`MIN_BAND_TOKENS = 512`、head ≤ 512、tail はプリセット比率（fidelity 0.60 / balanced 0.45 / memorySaver 0.30）で最低 256。中央帯が 512 未満なら None（＝フォールバック）。 |
| `segment.py` | バンドから `Block("exact", head)` / `Block("latent", band)` の順序プランを生成。 |
| `writer.py` | Writer `meanpool-v1`。`model.model.embed_tokens` 等を探索して埋め込み、`max_latent` 個の連続窓へ平均プール（`ceil` 分割）。dtype を維持。層が見つからない/形が不正なら `WriterUnavailable`。 |
| `writer_registry.py` | id → (callable, fingerprint)。今は `meanpool-v1` のみ。 |
| `latent_store.py` | 内容アドレスのソフトトークンキャッシュ。`SHA256(band tokens + reader fp + writer fp + POLICY_VERSION)`。RAM LRU ＋ 任意で `mx.save`/`mx.load` の `.npy`（`persistentLatentCache`）。次元不一致・load 失敗は miss。 |
| `hybrid_cache.py` | `HybridKVCache` と key 構成（reader fp + writer fp + template fp + POLICY_VERSION + mlx-lm version + exact block hash + latent block hash）。**v1 は再利用しない**（`get` は常に miss、`put` は no-op）。27B 級ハイブリッドでの往復未実測のため。 |
| `hybrid_prefill.py` | Hybrid Prefiller。`make_prompt_cache(model)` に対し、exact ブロックは `model(tokens, cache=cache)`、latent ブロックは `model(placeholder, cache=cache, input_embeddings=soft[None])` を順に prefill。`input_embeddings` 未対応（`TypeError`）は `PrefillError`。 |
| `retrieval.py` | Retriever v1。Static Hybrid ではバンドが1個なので実質パススルー。`maxRetrievedSegments` の seam のみ。 |
| `runtime.py` | `AdaptiveRuntime.prepare(prompt_tokens) -> {cache, remaining(tail tokens), all_tokens, metrics}`。policy/writer/prefill の各 ms を計測。`FallbackToExact` 以外は外へ出さない。 |

## 3. adapter.py の分岐（OFF 時はバイト等価）

`MLXLMAdapter.stream()` で chat テンプレート適用後・プロンプトキャッシュ fetch 前に:

```python
adaptive_cfg = params.get("adaptiveMemory") or {}
if isinstance(prompt, str) and adaptive_cfg.get("enabled") and adaptive_cfg.get("softToken"):
    try:
        from .adaptive_memory import AdaptiveRuntime, FallbackToExact
        plan = AdaptiveRuntime(self.model, self.processor, adaptive_cfg,
                               cache_root=os.environ.get("MLXBAR_PROMPT_CACHE_ROOT")).prepare(self._encode(prompt))
        cache = plan["cache"]; kwargs["prompt"] = plan["remaining"]; kwargs["prompt_cache"] = cache
        prompt_tokens = plan["all_tokens"]; cache_tier = "adaptive"; adaptive_active = True
        yield {"type": "metrics", "adaptive_memory": plan["metrics"]}
    except FallbackToExact as exc:   # nothing yielded yet -> clean fallback
        ...
    except Exception as exc:          # never break a request
        ...
```

すべてトークンを1つも yield する前に起きるので、失敗しても下の通常 EXACT 経路へ
そのまま落ちる。`adaptive_active` のときは既存の `PromptCacheStore.fetch` / `_remember`
（`store`）をスキップ（ハイブリッドキャッシュは Raw プロンプトの prefix ではないので
EXACT プロンプトキャッシュへ混ぜてはならない）。

## 4. リクエスト統合（オプトイン・互換影響ゼロ）

`openai_compat.chat()` と `anthropic_compat._messages()` の `maybe_compress_messages`
呼び出し直後に `maybe_plan_adaptive_memory` を1か所追加（`contextCompression` の後、
RAG の前）。返った `adaptiveMemory` dict を `options` へマージ。ワーカーの `metrics`
イベントに `adaptive_memory` があれば `AppState.last_adaptive_memory` を更新。

可視化: `AppState.last_adaptive_memory`（インメモリのみ、`last_context_compression` と
同じ扱い）→ `/api/v1/status` の `adaptiveMemory` フィールド → メニューバーに1行
（`compression_ratio` か `fallback_reason`）。

## 5. 設定スキーマ（追加のみ、`schemaVersion` は 1）

`DEFAULTS["experimental"]["adaptiveMemory"]`:

```json
{"enabled": false, "policy": "balanced", "triggerRatio": 0.60,
 "memoryPressureRatio": 0.75, "keepTailMessages": 8, "maxLatentTokens": 256,
 "maxRetrievedSegments": 8, "verbatimProtection": true, "softToken": false,
 "persistentLatentCache": true, "persistentHybridKV": true, "fallbackToExact": true}
```

`_validate`（`contextCompression` と同じ書き方）: bool 群、`policy ∈ {fidelity,
balanced, memorySaver}`、`triggerRatio` / `memoryPressureRatio` 0.5–0.95、
`keepTailMessages` 2–50、`maxLatentTokens` 16–2048、`maxRetrievedSegments` 1–64。
**排他**: `experimental.adaptiveMemory.enabled` と `contextCompression.enabled` が
両方 true なら `ValueError`。

## 6. CLI（`cli.py`、v1.8.1 パリティ契約）

`config set-adaptive-memory [--enabled …] [--policy …] [--trigger-percent …]
[--memory-pressure-percent …] [--keep-tail …] [--max-latent-tokens …]
[--max-retrieved-segments …] [--verbatim-protection …] [--soft-token …]`
（`set-context-compression` と同じ「指定した項目だけ変更」）。汎用 `config set
experimental.adaptiveMemory.<key> <value>` も従来どおり使える。

## 7. GUI（`MLXBarSettingsView.swift`「詳細」タブに「実験的機能」セクション）

マスタートグル（OFF）/ 方針 Picker（忠実度優先・バランス・メモリ節約）/ Stepper
（発火の目安 %、メモリ逼迫の目安 %、EXACT のまま残す直近ターン数、LATENT ブロックの
最大ソフトトークン数、取得する LATENT セグメントの上限）/ トグル（逐語保護、
ソフトトークンの Hybrid Prefill）/ 適用ボタン / 注意書き。`MenuBarViewModel` に
`setAdaptiveMemorySettings(...)`（サーバと同じ範囲ガード）、設定リフレッシュでの
`experimental.adaptiveMemory` パース、`/api/v1/status` の `adaptiveMemory` を
`lastAdaptiveMemoryRatio` / `lastAdaptiveMemoryFallbackReason` へ防御的にパース、
`adaptiveMemorySummaryText`。`MenuBarView` に1行（`sparkles`）。

**日英完全対応:** 新規 JP キーはすべて `en.lproj/Localizable.strings` に EN 訳を追加
（`ja` はソース言語なので追記不要）。排他エラーの `message` は日英併記。

## 8. 安全性・不変条件

- **`enabled = false`（既定）で完全休眠。** 通常リクエストは `maybe_plan_adaptive_memory`
  の enabled チェックで即戻る。adapter.py の分岐は `enabled` かつ `softToken` の二重ゲート。
  OFF 時の生成経路はバイト等価（`adaptive_active` は常に False）。
- **クラッシュ安全性:** 層1は `_plan` 全体を `except Exception -> (messages, None)` で包む。
  層2は `prepare()` が `FallbackToExact` 以外を投げず、adapter がそれを捕捉してトークンを
  1つも出す前に通常経路へ落ちる。Worker プロセスは死なない。実験機能の失敗を API エラーへ
  波及させない（§元設計 11）。
- **メモリ安全性:** ソフトトークンは `[≤maxLatentTokens, hidden]` の1配列のみ。既存の
  EXACT プロンプトキャッシュへハイブリッドキャッシュを混ぜない。ノート/ソフトトークン
  キャッシュは件数・バイトで上限管理し LRU/prune。
- **互換性:** `/api/v1/status` の `adaptiveMemory` は増分（旧 GUI は無視）。設定は追加のみ、
  `schemaVersion` 1 のまま、既存 `config.json` はそのまま読める。`adaptiveMemory` を
  含まない OpenAI/Anthropic リクエストは v2.1.0 とバイト等価。mlx-vlm Worker は無変更。
- **フォールバック理由:** Writer 非対応 / Reader 非対応（`input_embeddings` 不可）/
  埋め込み次元不一致 / Writer 実行失敗 / Latent Cache 破損 / chat テンプレート解析失敗 /
  Policy 内部例外 / プロンプトが短くバンド分割不可 — いずれも通常 EXACT 推論へ自動復帰。

## 9. 検証

`TEST_PLAN_v2.2.0.md` を参照。Python 回帰 **466件**（v2.1.0 の 430 + `test_adaptive_memory.py`
34 + `test_cli.py` 2）。固定順・ランダム順とも緑。`swift build --disable-sandbox` 成功。
実機で「無効時の回帰なし」「有効化して長い会話を送り `/api/v1/status.adaptiveMemory` に
`compression_ratio` が出て応答が一貫」「`softToken` ON で対応モデルは Hybrid Prefill、
非対応モデルは自動フォールバック」「GUI が日英で崩れない」を確認する。
