# MLXBar v1.6.2 複数モデル常駐設計書

更新日: 2026-08-24  
対象: Apple Silicon / macOS 14以降 / MLX LM / MLX VLM / LM Studio Provider

## 1. 目的と非目的

選択済みフォルダで発見したMLXモデルを、OpenAI互換APIの`model`指定に応じて必要時にロードする。モデル単位の上限、全体上限、実メモリ圧、常駐数の全条件を満たす場合だけ複数常駐させる。APIが自動ロードした非固定モデルは無使用TTL後に解放する。

この版は同時生成数を増やさない。MLXはユニファイドメモリを使い、複数モデルの同時計算ピークは測定なしに安全と証明できないため、生成は従来どおり全体で1件に直列化する。

## 2. 2026年8月の一次情報

- MLX 0.32.1の`mlx.core.set_memory_limit(bytes)`はグラフ評価時の上限ガイドであり、RAM/swapがなければ割り当ては例外になる。したがってこれは予約制御の代用ではなく、最後のアロケータ防壁として使う。[`set_memory_limit`](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.set_memory_limit.html)
- MLXの`get_active_memory`はキャッシュバッファを含めない。よって`active + cache`とOSのRSSを別々に見て大きい方を安全判定に使う。[`get_active_memory`](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.get_active_memory.html)
- `set_wired_limit`はmacOS 15以降でのみ有効で、総メモリ未満でなければならない。macOS 14も対象のMLXBarでは総量制御の根拠にしない。[`set_wired_limit`](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.set_wired_limit.html)
- LM StudioはJITロード、モデル単位TTL、Auto-Evict、複数instanceを提供する。v1 REST APIのload応答は`instance_id`を返すが、MLXBarのMLX allocatorと同等のbyte単位予約契約ではないため、LM StudioをMLX native poolの合計に入れない。[TTL / Auto-Evict](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict) / [load API](https://lmstudio.ai/docs/developer/rest/load) / [multiple instances](https://lmstudio.ai/docs/typescript/manage-models/loading)
- Ollamaは空きメモリへfitする場合に複数モデルをロードし、fitしなければFIFO待ちとidle model解放を行う。`OLLAMA_MAX_LOADED_MODELS`、`keep_alive`、queue上限が別契約であるため、MLXBarも常駐数・TTL・メモリ予算・queueを分離する。[Ollama FAQ](https://docs.ollama.com/faq)
- 2026-08-24の安定版確認基準はMLX 0.32.1、mlx-lm 0.31.3、mlx-vlm 0.6.15。特定アーキテクチャ名によるpool可否分岐はしない。[MLX 0.32.1](https://pypi.org/project/mlx/0.32.1/) / [mlx-lm 0.31.3](https://pypi.org/project/mlx-lm/0.31.3/) / [mlx-vlm 0.6.15](https://pypi.org/project/mlx-vlm/0.6.15/)

## 3. アーキテクチャ

```text
OpenAI API / GUI / CLI
          |
          v
 ModelPoolSupervisor (catalog resolve, admission, TTL, LRU, leases)
          |
          +-- global load lock (cold load = 1)
          +-- global generation lock (generation = 1)
          |
          +-- model A -> WorkerSupervisor -> process/socket -> mlx-lm or mlx-vlm
          +-- model B -> WorkerSupervisor -> process/socket -> mlx-lm or mlx-vlm
          +-- model C -> WorkerSupervisor -> process/socket -> mlx-lm or mlx-vlm
          |
          `-- LM Studio -> legacy provider path (native poolと排他)
```

従来の`WorkerSupervisor`は変更を最小限にし、poolは複数の従来Supervisorを合成するfacadeとする。pool無効時はリクエストをv1.6.1の単一Worker経路へそのまま委譲する。各モデルを別プロセスにするのは、可変の上流ランタイムのglobal state、Metal allocator、Python例外/クラッシュをモデル単位に封じ込めるためである。

## 4. 設定契約

`models.pool`:

| 項目 | 既定 | 範囲 | 意味 |
|---|---:|---:|---|
| `enabled` | `true` | bool | プロセス構成なので次回service起動時に反映 |
| `maxResidentModels` | 2 | 1–8 | native MLX常駐数の上限 |
| `totalMemoryRatio` | 0.75 | 0.50–0.90 | 物理メモリ比の全体上限 |
| `minimumSystemReserveGB` | 4 | 1–128 | OS/他アプリに必ず残す量 |
| `defaultPerModelMaxGB` | 32 | 1–512 | 1モデルの既定上限 |
| `idleTTLSeconds` | 900 | 30–86400 | API自動ロードモデルの無使用時間 |
| `loadConcurrency` | 1 | 1のみ | ロードピークを重ねない |
| `profiles` | `[]` | 最大64 | `modelId`, `maxMemoryGB`, `keepLoaded` |

全体予算は`min(physical * totalMemoryRatio, physical - minimumSystemReserveGB)`。冷間見積もりはモデルweightの実ファイル合計に35%とプロセス固定費512 MiBを加える。見積もりは精密な予測ではなく事前拒否用の保守的chargeで、ロード後は`max(RSS, MLX active + cache)`で予約を引き上げる。

## 5. アドミッションと解放

ロードは次をすべて満たす場合のみ行う。

1. カタログに解決でき、対応engineがmaintenance中でない。
2. 安全見積もり ≤ 個別上限。
3. 安全見積もり ≤ 全体予算。
4. macOSがwarning/criticalのメモリ圧を報告していない。
5. reclaimable memoryが見積もり+予備を下回らない。
6. idleかつ非固定のLRUを先に外せば、常駐数と全体予算の両方へ収まる。
7. Worker起動後、そのプロセスが`set_memory_limit`を予約byteで適用したことをcapability応答で確認できる。
8. ロード後の実測値が個別上限と全体予算を超えない。

同じmodel IDの同時要求はsingleflightで1回だけロードする。異なるmodel IDのロードもglobal load lockで直列化する。失敗時はWorkerを必ず停止し、slotと予約を削除し、元の例外をすべてのwaiterへ返す。

生成開始前にleaseを増やし、streamの正常終了、client切断、キャンセル、例外のどの経路でも`finally`で減らす。lease > 0のslotはTTL/LRU/設定縮小で解放しない。非固定slotは最終lease解放時からTTLを数える。critical pressureではマシン全体を守るため、idleであれば固定指定も解放可能とする。

## 6. ランタイムと新モデルへの耐性

- モデル名、パラメータ数、architecture名のallowlistで判定しない。既存カタログの実ファイル量、既存runtime probe、Worker契約だけを使う。
- 新runtime slotはimport/stream契約に加え、`mlx.core.set_memory_limit`の存在をstage中に必須確認する。契約のないslotはactivateしない。
- runtime更新時は対象engineの常駐モデル一覧とpinをsnapshotし、対象engineだけをdrain→切替→probe→再ロードする。失敗すれば旧slotへ自動rollbackし、同じ一覧を戻す。
- poolの有効/無効は実行中に構成を入れ替えず、次回service起動時に反映する。その他の上限縮小はidle LRUから即時適用する。
- CoordinatorがSIGKILLされても、model単位manifestのPIDと実コマンドを照合し、次回起動でTERM→KILLとscoped socket削除を行う。PIDの再利用だけで無関係なプロセスを停止しない。

## 7. 後方互換性

- 従来の`loadedModel`は「直近に選択/要求されたprimary」として維持し、`loadedModels`/`modelPool`を追加する。従来clientは変更不要。
- `/v1/models`は複数のloaded stateを報告し、Chat Completionsは解決したmodel IDのWorkerへルートする。OpenAI互換の形は変えない。
- pool無効時は従来Supervisorへ委譲。LM Studioは従来Provider経路のままで、native poolと同時常駐させない。
- 設定は既存schema 1のdeep mergeで追加され、v1.6.1のconfigは変換なしで読み込める。

## 8. エラー契約

| code | HTTP | retryable | 意味 |
|---|---:|---:|---|
| `MODEL_MEMORY_LIMIT` | 409/503 | 条件による | 事前見積もりまたは実測が個別上限超過 |
| `MEMORY_BUDGET_EXCEEDED` | 503 | true | 全体予算、常駐数、現在空きのいずれかで不可 |
| `MEMORY_PRESSURE` | 503 | true | macOSがメモリ圧を報告 |
| `RUNTIME_MEMORY_LIMIT_UNAVAILABLE` | 409 | false | ランタイムがallocator上限契約を証明できない |
| `ENGINE_BUSY` | 409 | true | update中または安全に構成を切り替えられない |

## 9. 観測と受け入れ基準

`GET /api/v1/status`に常駐モデル一覧、slot state、予約byte、lease数、TTL時刻、固定状態、pool合計/予算、再起動要否を出す。GUIには常駐数、予約合計、予算を表示する。

リリースの必須条件は次のとおり。

1. 同一modelのsingleflight、複数Worker、LRU、TTL、pin、critical pressure、個別上限事前拒否、allocator適用確認、stream中断時lease解放の自動テスト。
2. v1.6.1までの全テストを含む回帰テスト。
3. Swift Debug/Release build。
4. ad-hocまたはDeveloper ID署名済みapp/DMGの検証。
5. 実MLX 0.32.1でWorkerが適用したbyteをhealth/load契約で返すこと。実重みの複数常駐は対象Macに十分な空きがある場合にのみ行い、実施できない検証は「未実施」と明記する。

