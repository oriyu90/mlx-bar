# MLXBar v1.6.0

MLXBar v1.6.0 makes an interrupted conversation cheap to resume. Cancelling a generation used to throw away the entire prompt cache, so the next request re-processed the whole conversation from the first token — on a 100,000-token ZCode session with `Qwen3.8-27B-MLX-8bit`, that is close to eight minutes of silence before the first character appears. The work that was already done is now kept.

MLXBar v1.6.0は、中断した会話の再開を安くします。これまで生成をキャンセルするとプロンプトキャッシュが丸ごと破棄され、次の要求は会話の先頭から計算し直していました。`Qwen3.8-27B-MLX-8bit`で10万トークンのZCodeセッションでは、最初の1文字が出るまで8分近い無言になります。すでに終わっている計算を、捨てずに使います。

## The measurements this release started from / この版の出発点になった実測

From one ZCode session's API log, same model, same conversation:

| tier | prompt | cached | first token |
|---|---|---|---|
| memory | 71,039 | 70,798 | **1.9 s** |
| disk | 73,331 | 12,228 | 257 s |
| cold | 104,567 | **0** | **477 s** |

Every request after 16:58 was cold. Nothing in the interface said so.

同じモデル・同じ会話のAPIログです。16:58以降のすべての要求がcoldに落ちていましたが、そのことを示す表示はどこにもありませんでした。**v1.6.0の半分は、この「静かな劣化」を見えるようにするための変更です。**

## An interrupted generation keeps its cache / 中断した生成はキャッシュを保つ

The runtime advances a reused cache in place, but it only writes the matching token ids back when a generation runs to the end. An interrupted turn therefore left the cache one step ahead of its own labels, and the only safe thing to do with it was to throw it away.

MLXBar now records the prompt's token ids and each generated token as they stream, and re-pairs the two when a turn is cut short. **The rewrite happens only when the cache can be shown to hold exactly the tokens being claimed for it** — the reported offset has to equal prompt + generated. Where it does not, or where the runtime reports no offset at all, the cache is discarded exactly as before. An unprovable pairing is silent corruption, which is worse than a slow request.

ランタイムは再利用したキャッシュをその場で進めますが、対応するトークンIDを書き戻すのは生成が最後まで走ったときだけです。中断したターンではキャッシュだけが1手先に進んだ状態になり、安全な選択肢は破棄しかありませんでした。v1.6.0はプロンプトのトークンIDと生成トークンを逐次記録し、中断時に対応を組み直します。**書き換えるのは「キャッシュが、主張どおりのトークンを保持していると証明できるとき」だけです**（報告されるoffsetがプロンプト＋生成と一致すること）。一致しない場合や、そもそもoffsetを報告しないランタイムでは従来どおり破棄します。証明できない対応付けは、遅い要求よりも悪い「静かな破壊」だからです。

Two interruptions are treated differently on purpose. A cancellation wants the work kept; the memory watchdog stopping the same request wants it released, because releasing memory is the whole point of that interruption.

中断の種類で扱いを分けています。キャンセルは作業を残したい中断ですが、メモリ監視による停止はメモリを空けるための中断なので、保持は目的に反します。

## Reuse that does not depend on `trim` / `trim`に依存しない再利用

Hybrid attention models — Qwen3.5, Qwen3.8, and anything else mixing recurrent layers with full attention — cannot roll a cache back to a shorter prefix. A recurrent state has no "last N tokens" to remove. mlx-vlm 0.6.15 guards its rollback with a retention check that does not test for this, so a branching conversation reached `c.trim(n)` on a component that has no `trim` at all and fell back to a full prefill (`'ArraysCache' object has no attribute 'trim'`).

MLXBar now answers that question itself, **before** the runtime reaches the call, by asking the cache objects what they can do:

- **Continuation** — the retained cache is already the shared prefix. Every architecture reuses it.
- **Branch, cache can trim** — left to the runtime, unchanged.
- **Branch, cache cannot trim** — MLXBar restores a snapshot of the last completed turn instead. Failing that, it declines the reuse cleanly rather than letting the runtime raise.

The snapshot uses the same `state` contract that cache serialisation is built on, so it works for a recurrent component that will never gain a `trim`. Nothing keys off a model name, an architecture string or a runtime version; **if upstream adds the missing method, MLXBar starts using the cheaper path with no change here.**

ハイブリッド構成のモデル（Qwen3.5 / Qwen3.8 など、再帰層と全注意層が混在するもの）は、キャッシュを短いprefixへ巻き戻せません。再帰状態に「末尾Nトークン」という概念がないためです。mlx-vlm 0.6.15の巻き戻しガードはこの条件を見ていないため、会話が枝分かれすると`trim`を持たない要素に対して`c.trim(n)`へ到達し、cold prefillへ落ちていました。v1.6.0はこの判断をランタイムより手前でMLXBar自身が行います。判定材料は**メソッドの存在**だけなので、上流が修正すれば自動的に安い経路へ戻ります。

## Cache size is arithmetic, not a constant / キャッシュ容量は定数ではなく計算

A snapshot's size comes from the model, not from MLXBar: for `Qwen3.8-27B-MLX-8bit` it is **64 KB per token**, because 16 of its 64 layers are full attention with 4 KV heads of width 256. A 100,000-token conversation therefore needs **6.4 GB**, and a 5 GB disk limit cannot hold one — so every write was evicted immediately and the disk tier never returned more than an early 12,000-token prefix. That is exactly what the table above shows.

MLXBar now derives the figure from the model's own `config.json`, states it when the model loads, and **turns the disk tier off with a reason rather than writing gigabytes that are certain to be evicted.** A new `scripts/cache-capability-matrix.py` reports the same arithmetic for every local model without loading any weights:

```
model                     rollback    per token  context    snapshot  fits 10 GB
Qwen3.8-27B-MLX-8bit      checkpoint  64.0 KB    262,144    16.1 GB   no (holds 161,491)
llm-jp-4-32b-a3b-4bit     trim        64.0 KB    65,536     4.0 GB    yes (holds 163,840)
```

スナップショットの大きさはMLXBarではなくモデルが決めます。`Qwen3.8-27B-MLX-8bit`は64層のうち16層が全注意（KVヘッド4・幅256）なので**1トークンあたり64 KB**、10万トークンでは**6.4 GB**です。ディスク上限5 GBでは1件も収まらず、書いては即座に捨てる状態になり、ディスク層は序盤の約12,000トークンしか返せませんでした。v1.6.0は`config.json`から算出し、ロード時に提示し、**収まらないと分かった時点で理由付きで書き込みを止めます。**

## Saying when reuse has stopped working / 再利用が止まったことを伝える

Every number needed to notice the failure above was already being recorded. None of it was shown.

- The menu bar states what reuse the loaded model gets, and warns after two consecutive full prefills, with the reason.
- `cache_tier: cold` now carries a reason: `reuse_unsupported`, `budget_insufficient`, `cancelled_previous`, `memory_pressure`, `token_ids_unavailable`, `write_budget_reached`, `runtime_changed`, `no_prefix`, `first_request`. It is a closed vocabulary — an unrecognised value is stored as NULL, never written through.
- A long prefill reports an estimate, from this worker's own measured rate and tokenizer ratio. Eight minutes of silence and eight minutes with "about six more to go" are the same wait, but only one is survivable.
- The shared prefix length is reported per request, which is how a client discovers that something at the front of its prompt — a timestamp in a system prompt, a non-deterministic tool order — is destroying reuse.
- `GET /v1/models` advertises `prefix_reuse` and `recommended_max_prompt_tokens` for the loaded model.

No conversation text, tool definition or key is stored for any of this.

上の失敗を検知するのに必要な数値は、すべて既に記録されていました。表示していなかっただけです。cold の理由は閉じた語彙で、未知の値はNULLとして保存します。会話本文・tool定義・キーは一切保存しません。

## Also in this release / その他

- **The last model is reloaded after a restart.** Skipped when you unloaded it yourself — that flag is the difference between "MLXBar restarted" and "I turned this off".
- **Switching a runtime slot now refuses to interrupt a generation**, the same way an explicit unload already did.
- **A change in reuse capability is recorded in the runtime history**, so an upstream fix taking effect is visible as one line.
- `promptCache.branchCheckpoint` (`auto`/`off`), `promptCache.diskWriteBudgetGB` (default 32) and `promptCache.memoryBlocks` (`auto`/`off`, **default off**) are new settings. The block pool stays off because its behaviour on a 27B-class hybrid has not been measured, and an unmeasured default is not one worth shipping.

## Upstream / 上流

`mlx_vlm/generate/dispatch.py` guards its rollback with `_cache_fully_retained()`, which returns True for a component that defines no `trim` at all. Adding a `hasattr(c, "trim")` check there fixes every hybrid architecture at once. MLXBar no longer depends on it, but the fix belongs there.

## Verification / 検証

- 254 Python tests pass, including 38 covering interruption, branching, budget arithmetic and snapshot round-trips.
- Swift release build succeeds; the packaged coordinator reports `1.6.0` from `/api/v1/health`.
- Capture, restore and the reuse policy are verified against **real** `KVCache`, `ArraysCache` and `PromptCacheState` objects from mlx-vlm 0.6.15 — no weights required, so this runs every release. A snapshot demonstrably does not follow the live cache forward.
- **Not yet verified: interruption and resume with a model actually loaded.** Every failure on that path degrades to a cold prefill — v1.5.3's behaviour — except snapshot restore, which is the one route that could change output. Run section 2-1 of `TEST_PLAN_v1.6.0.md` (a 9 GB model reproduces the same hybrid architecture) before relying on it.

- Pythonテスト254件成功。パッケージ済みCoordinatorが`/api/v1/health`で`1.6.0`を返すことを確認。スナップショットの取得・復元と再利用方針は、mlx-vlm 0.6.15の**実際の**キャッシュクラスに対して検証済み（重み不要）。**実モデルを載せた状態での中断→再開は未検証**。この経路の失敗は原則cold prefillへ退避する（＝v1.5.3と同じ）が、スナップショット復元だけは出力が変わりうるため、常用前に`TEST_PLAN_v1.6.0.md`の2-1を通すこと。

## Install / インストール

Download `MLXBar-1.6.0.dmg`, open it, drag MLXBar to Applications. The build is ad-hoc signed, so the first launch needs System Settings → Privacy & Security → Open Anyway.

`MLXBar-1.6.0.dmg`をダウンロードして開き、MLXBarをアプリケーションへドラッグします。ad-hoc署名のため、初回起動は「システム設定 → プライバシーとセキュリティ」から許可してください。

SHA-256: `b7155eb3b5d336550fbd66d074172d8e6c48ef6ebf8eda229fc16947eb3b9ea6`
