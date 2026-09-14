# MLXBar: oMLX型KVキャッシュ管理の調査・採用判定・実装計画

更新日: 2026-09-15
対象: `oriyu90/mlx-bar` v2.3.0
文書の性格: 調査・採用設計と、2026-09-15時点のPhase 1/2実装記録。

> 実装状態: Phase 1とPhase 2（mlx-lm plain `KVCache`のSSD block reuse）を実装済み。
> Phase 3～5は本文の実機ゲートを満たしていないため、設計どおり未実装・無効のままとした。

## 1. 結論

oMLXのようなKVキャッシュ管理機能はMLXBarに搭載できる。ただし、oMLXの実装を丸ごと移植するべきではない。

採用価値が高いのは、次の5点である。

1. 固定長ブロックと連鎖ハッシュによる分岐prefix再利用。
2. モデル・重み・トークナイザ・chat template・MLX/MLX LM版・KV型配列の完全なnamespace分離。
3. KV型ごとの能力判定と、不明な型を必ず非対応とするfail-closed方針。
4. SSD容量、保留RAM、書き込み待ちバッファ、復元時一時メモリを個別に上限管理すること。
5. 破損、非互換、メモリ圧、I/O失敗をキャッシュmissに降格し、最初の出力前に従来のEXACT prefillへ一度だけ戻すこと。

初期実装の対象は **mlx-lmの、全layerが安全にblock slice/restoreできるモデルだけ** とする。mlx-vlmはすでに上流`mlx_vlm.apc` のSSD APCを利用しており、置換の利得より互換性リスクが大きい。再帰/rolling/composite cacheは当面、既存のexact snapshot/checkpointに委ねる。

最終提案は以下である。

- `experimental.pagedKVCache.enabled = false`を新設し、未設定/無効時は v2.2.0 と同じ経路にする。
- 実装は既存`PromptCacheStore`の背後に選択的backendとして追加し、既存memory LRU/disk snapshotをrollback先として残す。
- 第1版はSSD block tierのみ。RAM hot block tier、KV quantization、continuous batching、soft-token hybrid KV永続化は実機計測後の別phaseにする。
- 全モデルで有効と表示せず、モデルload時のcapability probeで`eligible / checkpoint_only / unsupported`を確定する。
- 有効化後も、そのrequestで安全を証明できなければ自動的に従来経路を使う。

## 2. 調査対象と前提

### 2.1 読んだ実ソース

- MLXBar: `oriyu90/mlx-bar` `2ee6ee8` (v2.2.0)。
- oMLX: `jundot/omlx` `b390b31e0c6831225fed0f24d278eb1db7fcb68b` (2026-09-11, `0.7.0.dev2`)。
- mlx-lm: `ml-explore/mlx-lm` `e5962529e5614ce00f14bdd39fe5fc6e410ca2b0`。
- mlx-vlm: `Blaizzy/mlx-vlm` `cdc745ad8a32d162f6d8e9d08be256910d663ac2`。
- 非公開保守情報: `common-rules-document/common rules.md`、`common-rules-document/mlx-bar.md`。

oMLXの`pyproject.toml`はmlx-lm/mlx-vlmを特定commitに固定している。対してMLXBarは、runtime slotを安定版または指定commitへ別々に更新できる。この差のため、oMLXのクラス名別処理をMLXBarにそのまま持ち込むと、ランタイム更新後の静かな誤復元が起き得る。

### 2.2 安全性の定義

本設計の「安全」は、次のすべてを満たすことを指す。

- キャッシュの不具合でWorker/Coordinator/GUIがクラッシュしない。
- キャッシュの誤復元で、別のtoken列・モデル・画像・テンプレートのKVを接続しない。
- キャッシュのためにメモリ上限、SSD上限、書き込み上限を超えない。
- 不明なランタイム・モデル・cache classは「互換と推定」せず「最適化なし」へ倒す。
- 無効時は既存モデル、API、ランタイム、キャッシュディレクトリ、レイテンシに変化を与えない。

## 3. MLXBarの現状

### 3.1 mlx-lm

`Workers/mlx_lm_worker/prompt_cache.py` の`PromptCacheStore`はすでに2層である。

- RAM: mlx-lmの`LRUPromptCache`。token trieから最長prefixを得る。物理RAM比の`memoryRatio`で上限を与える。
- SSD: 同一prompt形状ごとの完全snapshot。最新tail 256 tokenをguardし、safetensorsで保存する。
- namespace: resolved model path、runtime version、config/tokenizer/template、weight fileのsize/mtimeをSHA-256に含める。
- 回復: 読めないsnapshotを削除しmissにする。
- 中断: cache offsetと`prompt + generated`の長さが一致すると証明できた場合だけ保留する。memory pressure中断は保留しない。

不足しているのは、SSD snapshotがブロック単位ではないため、会話分岐・tool resultの変更・複数クライアントで巨大snapshotを丸ごと再保存しやすい点である。

### 3.2 mlx-vlm

mlx-vlm経路は、上流runtimeの`mlx_vlm.apc.APCManager` / `DiskBlockStore`を既に使う。

- `PromptCacheState`をwarm tierとして維持。
- `APCManager(num_blocks=0)`を既定とし、大容量KVのRAM二重保持を避けながらSSD tierを利用。
- `promptCache.memoryBlocks = auto`のときだけ、モデルのKV bytes/tokenと物理RAM比からblock数を算出。既定は`off`。
- hybrid/recurrent modelのrollbackはtrimではなくcheckpointの複製と復元で対応。
- runtime更新とモデル差し替えでnamespaceを分離。

したがって、MLXBarで主に解くべきなのは **mlx-lm側のbranch-awareなSSD block reuse** であり、mlx-vlm側をoMLX独自cacheで二重化することではない。

### 3.3 Adaptive Hybrid Context Memoryとの関係

v2.2.0の`Workers/mlx_lm_worker/adaptive_memory/hybrid_cache.py`はkey構成だけを持ち、`get`は必ずmiss、`put`はno-opである。これは正しい安全側の状態である。

oMLXのcacheは「同一の生token列のexact KV」を再利用する。一方、Adaptive Memoryのsoft token経路は、元テキストと異なる埋め込み列をKVにする。両者は同じstoreへ混ぜてはならない。

soft-token KV再利用は、次の第三のnamespaceとして後日実装する。

```
raw exact cache      = raw token chain + reader/model/runtime/layout
coordinator notes    = source text + policy/summarizer
soft-token hybrid KV = exact head + latent band + writer + reader + template + policy + runtime
```

`persistentHybridKV: true`は既存設定に存在するが、将来実装時に突然再利用を開始させない。新たに`hybridKVReuse: false`を追加し、`adaptiveMemory.enabled && softToken && persistentHybridKV && hybridKVReuse`の4重gateが揃った場合だけ使う。

## 4. oMLXの実装分析

### 4.1 主要構造

oMLXのcache stackは大きく以下からなる。

| 要素 | 実装上の役割 | MLXBarでの判定 |
|---|---|---|
| `PagedCacheManager` | block metadata、LRU free queue、refcount、hash→block、COW | 連鎖hashとLRUは採用。COW/refcountは後回し |
| `BlockAwarePrefixCache` | 最長prefix検索、block store/reconstruct、型検証 | 縮小版をmlx-lmに新規実装 |
| `PagedSSDCacheManager` | safetensors、hot/raw-byte tier、background writer、LRU、容量制御 | 原理は採用。初版は同期・少数bufferに縮小 |
| type registry/handlers | KV/rotating/arrays/composite/独自cacheの抽出・分割・復元 | 「対応を明示登録し、不明は拒否」を採用 |
| boundary snapshots | block sliceできない再帰stateをprefix境界で保持 | 必要だが後日。既存checkpointを維持 |
| scheduler integration | continuous batching、prefill chunk、decodeとstoreの協調 | MLXBarには移植しない |
| memory monitor | 復元/保留/prefillの複数フットプリントを事前予約 | 既存MLXBar guardと統合して採用 |

oMLXの`omlx/cache/`だけで約18,576行あり、さらに`scheduler.py`の広範囲と結合している。このコード量は「そのまま移植すれば高機能」という意味ではなく、モデル固有cacheやbatch schedulerへの適応がそれだけ難しいことを示す。

### 4.2 優れている点

#### 連鎖ハッシュ

block hashに直前block hash、そのblockのtoken IDs、model identity、画像等のsemantic keyを含める。途中の1 blockが違えば、それ以降の連鎖は別物になる。クライアントがconversation IDを持たなくても、同一prefixを安全に共有できる。

#### capabilityの交差

hybrid modelはlayerごとのcache性質が異なる。oMLX/mlx-vlm APCは、すべてのcomponentが同じtoken境界へ正しく復元できる場合のみ再利用する。途中のlayerを無視して「部分的に似ている」状態を実行しない。

#### 起動後の自己検査

モデルが作る空cache実体からlayer能力を調べ、clone/restore契約を確認する。モデル名のallowlistより、ランタイム更新と新規architectureに強い。これはMLXBarの既存保守方針と一致する。

#### 容量と一時バッファの分離

保留済みhot cache、SSD、未書き込みraw buffer、prefill復元のための一時メモリは異なるリソースである。oMLXはこれらを別々に計上し、prefill前に余白を作る。

### 4.3 そのまま採用できない理由

oMLXの近期のissue/修正は、次の失敗モードを実際に示している。

- TurboQuantとhybrid ArraysCacheのcache type signature不一致で、全requestがcoldになる。
- trailing partial blockの扱いが誤ると、system promptや最新user messageを落とした状態で推論し、品質劣化や思考漏洩を招く。
- 復元時のposition/RoPE/dtype/boundary不一致が、記号の無限反復のような静かな破損として現れる。
- 異なるthreadでMLX tensor load/concatenateとdecodeを同時に行い、Metal command buffer raceでprocessがabortする。
- 非互換/古いblockを容量計上しないと、SSD上限を超える。
- 同一prefixの差分だけではなく完全snapshotを複数持つと、RAM/SSD両方で容量がターン数に比例する。

これらは「oMLXが危険」という結論ではない。KV cacheの部分復元が、出力を返すだけの通常機能よりはるかに広い状態空間を持つことの証拠である。MLXBarでは対応範囲を狭く始め、能力の証明を増やす方向でのみ対応モデルを広げるべきである。

### 4.4 ライセンス

oMLXはApache-2.0、MLXBarはMITである。Apache-2.0 codeの複製/改変を含める場合はApache-2.0のLICENSE交付、変更表示、必要なNOTICE維持が必要になる。本提案は、oMLX/vLLMのアーキテクチャ上の考え方を参考にしつつ、MLXBarの小さいworker契約に合わせて独自実装する。コードまたは非自明な実装表現を転用する場合は、別途Apache-2.0 attributionを追加する。

## 5. 機能別の採用判定

| 機能 | 価値 | リスク | 判定 |
|---|---:|---:|---|
| stable chained token hash | 高 | 低 | Phase 1で採用 |
| SSD block dedup/分岐prefix | 高 | 中 | Phase 2でmlx-lm限定採用 |
| cache capability self-check | 非常に高 | 低 | Phase 1で採用 |
| 互換signatureと自動無効化 | 非常に高 | 低 | Phase 1で採用 |
| corrupt block quarantine + exact retry | 非常に高 | 中 | Phase 2の必須条件 |
| 完全なcache統計/診断 | 高 | 低 | Phase 1で採用 |
| RAM hot block tier | 中 | 高 | 既定OFFでPhase 4候補 |
| background SSD writer | 中 | 高 | 初版では不採用 |
| COW/refcount | 低～中 | 中 | 同一Worker並行を導入するまで不採用 |
| continuous batching | 別目的 | 非常に高 | 不採用 |
| KV quantization/TurboQuant | 中 | 非常に高 | 本計画の対象外 |
| hybrid/recurrentの任意block連結 | 高 | 非常に高 | checkpointに限定、汎用連結は不採用 |
| VLM cacheの独自置換 | 低 | 非常に高 | 不採用。上流APCを使う |
| soft-token hybrid KV persistence | 将来価値あり | 非常に高 | 別のPhase 5、4重gate |

## 6. 提案アーキテクチャ

### 6.1 基本原則

`PromptCacheStore` を外側のfacadeとして維持し、下にbackendを追加する。

```
MLXLMAdapter
  └─ PromptCacheStore (existing facade)
       ├─ MemoryPrefixBackend  = mlx-lm LRUPromptCache (existing)
       ├─ SnapshotBackend      = whole-prefix safetensors (existing)
       └─ PagedKVBackend       = new, opt-in, eligible layouts only
```

lookup順は以下とする。

1. 既存RAM LRU。
2. 新規paged SSD。
3. 既存whole-prefix SSD snapshot。
4. cold EXACT prefill。

paged経路の失敗が2、3、4の利用を妨げないことが重要である。

### 6.2 新規module

`Workers/mlx_lm_worker/paged_cache/`に以下を追加する。

| ファイル | 責務 |
|---|---|
| `types.py` | `CacheCapability`, `LayoutFingerprint`, `BlockDescriptor`, typed error |
| `capabilities.py` | 空cacheの型・state・meta_state・shapeの能力判定 |
| `hashing.py` | SHA-256連鎖hash、terminal domain separation、namespace fingerprint |
| `extract.py` | 対応済みcacheから指定blockだけをsliceして評価 |
| `restore.py` | 新鮮な`make_prompt_cache(model)`へblockを連結し、offset/shape/dtypeを検証 |
| `disk.py` | immutable blockのatomic write/read/quarantine/LRU prune |
| `store.py` | longest-prefix lookup、dedup store、統計、全失敗のmiss化 |
| `probe.py` | load時self-checkとprivacy-safeな診断値 |

oMLXの汎用handler registryをそのまま複製しない。初期版は、ランタイムが生成したcache実体に対して、次の契約を全layerが満たすときだけ対応する。

- `state`のgetter/setterが型に定義されている。
- state内のsequence axisが明示登録された対応classである。
- slice後の全layerが同じtoken countを報告する。
- 新規cache実体にstate/meta_stateを復元できる。
- 復元後の`cached_length`が対象prefixと完全一致する。

1 layerでも不明なら`checkpoint_only`または`unsupported`である。

### 6.3 block境界

初期実装のblock sizeは **256 tokenに固定** する。理由は、最新mlx-lmの`KVCache.step = 256`と整合し、ファイル数と復元回数を抑えられるためである。block sizeはformatの一部としてnamespaceに入れる。初版ではUIから変更できない。

通常blockは完全な256 tokenだけを保存する。ただし、最新tailを落としてモデルに見せないことは絶対にしない。cache hitで復元するのはfull blockまで、端数の最新tailは必ず通常prefillへ渡す。

system/toolsの安定prefixが256境界に揃わない場合に備え、現行のwhole-prefix snapshotを併存させる。terminal partial blockの独自kindはPhase 3で検討し、Phase 2では導入しない。

### 6.4 hashとnamespace

block hash:

```
H("mlxbar-paged-kv-block-v1\0" || parent_hash || token_ids_u32_le || semantic_salt)
```

namespace fingerprintには少なくとも以下を含める。

- format version。
- resolved model path。
- `config.json`、tokenizer config、chat template、safetensors indexの内容hash。
- 全weight fileのname/size/mtime ns。可能な場合はmodel revision/fingerprint。
- `mlx`、`mlx-lm`、Pythonのversion。
- cache layer数、各layerのclass qualified name、state tuple要素数、sequence axis、dtype/shapeの非sequence部分。
- KV quantizationを将来追加する場合はbits/group/scheme/start layer。未実装中は常に`none`。
- adapter/LoRAを将来許す場合はadapter identity。現時点で不明ならpaged cacheを無効化。

VLMの画像、audio/video、RoPE delta、model-specific semantic inputを正しくsalt化するのは難しい。そのため新規backendはmlx-lm text-onlyに限定し、VLMは上流APCに委ねる。

### 6.5 SSD formatとクラッシュ耐性

保存先:

```
<MLXBar root>/prompt-cache-paged/mlx-lm/<worker-instance>/<namespace>/<h0h1>/<hash>.safetensors
<same>/<hash>.json
<same>/quarantine/
```

原則:

- ファイル名は内容ハッシュからのみ作る。ユーザー入力をpathに使わない。
- root/subdirectoryは0700、fileは0600。symlink、非regular file、root外を拒否。
- 一時ファイルは同一directoryの`<hash>.tmp.<pid>.safetensors`。完了後にfile `fsync`、atomic rename、parent directory `fsync`。
- safetensors以外のpickle/object deserializationは使わない。
- metadataにformat、namespace、parent hash、token count、layer signature、各tensorのdtype/shape/byte length、payload SHA-256を持たせる。
- read時はheader上限、tensor数上限、ファイルsize上限を先に検査してからMLX配列を作る。
- 検証失敗fileはそのrequestから除外し、quarantineへatomic move。quarantine自体も上限内で古い順に削除。
- startup時に一定時間より古い`.tmp.*`だけを削除。他のnamespaceを無条件に削除しない。

初版は複数workerが同じmutable indexを書かないよう、`worker-instance`で保存先を分ける。レプリカ間のSSD block共有は価値があるが、複数processの同時書き込み、LRU更新、削除とreadの競合を新たに生む。後日、immutable shared blocks + Coordinator所有のmetadata/quota serviceとして別phaseで設計する。

### 6.6 メモリ安全性

初期版でRAM hot block tierを使わない。復元後KVと実行中KVを2本持つ時間を最小にする。

復元アドミッションは次を必須にする。

```
estimated_restore_bytes
  = fixed_recurrent_bytes
  + restored_tokens * measured_or_config_derived_bytes_per_token
  + one_block_decode_growth
  + bounded_deserialization_scratch
```

- `cacheBudget.known == false`ならSSD block restoreしない。従来snapshotまたはcoldへ戻す。
- 予測は過小評価しない。load後の実測bytes/tokenが大きい場合は、以後は実測の大きい方を使う。
- Supervisorの`memory_pressure_reason`と同じ、OS pressure、available memory、RSS、MLX active+cache、worker reservationの最も厳しい値を使う。
- メモリ圧時は新規restoreを禁止、RAM cacheを解放、`mx.clear_cache()`をMLX owner thread上で行い、まだ圧があれば従来どおり503とする。
- SSDからのblock load、concatenate、`mx.eval`は必ず生成と同じMLX worker threadで行う。Python background threadでMLX tensor操作を行わない。
- storeは1 blockずつslice→materialize→serialize→参照破棄する。複数巨大blockをqueueに積まない。
- Phase 2ではbackground writerを入れない。I/O latencyが受入基準を超えるなら、まず「今回の新規block保存数に上限」を入れ、無制限queueで隠さない。

### 6.7 プロセス間のSSD上限

新規`pagedKVCache.diskMaxGB`は **プロセスごとではなくMLXBar全体の上限** と定義する。初期版は、起動時の`maxResidentModels`分で安全側に均等分割したper-worker quotaを環境変数で渡す。

```
per_worker_disk_quota = global_paged_quota / max(1, latched_max_worker_count)
```

これは使用効率より「複数モデル/レプリカで上限がN倍にならない」ことを優先する初期解である。Phase 4でCoordinator管理の動的quota leaseへ進化させる。

## 7. 設定契約

### 7.1 追加schema

```json
{
  "experimental": {
    "pagedKVCache": {
      "enabled": false,
      "diskEnabled": true,
      "diskMaxGB": 10,
      "memoryTier": "off",
      "memoryRatio": 0.05,
      "branchReuse": true
    }
  }
}
```

契約:

- `enabled`: master switch。既定`false`。false/未設定の場合は新packageをimportせず、directoryを作らず、新しいprobeも走らせない。
- `diskEnabled`: paged backendのSSD保存。`enabled=true`でfalseの場合、capability/metricsのドライランだけを行いKVは保留しない。
- `diskMaxGB`: paged backend全体の上限。1～100 GB。既存`promptCache.diskMaxGB`とは別budget。将来統合する場合は明示migrationを行う。
- `memoryTier`: `off | auto`。初期既定`off`。`auto`の実動はPhase 4まで実装せず、非対応版ではvalidation errorにする。設定したのに動かない状態を作らない。
- `memoryRatio`: Phase 4用。0～0.20。
- `branchReuse`: block prefix共有。診断/A-B用にoffにできる。offではwhole-prefix exact hitだけを許す。

次はユーザーが無効化できる。

- paged KV cache全体。
- paged SSD tier。
- 将来のRAM hot tier。
- branch prefix再利用。
- Adaptive Memoryのsoft-token/hybrid KV再利用。

次の安全機能は無効化不可とする。これらは機能ではなく、キャッシュを利用するための前提である。

- namespace/signature検証。
- shape/dtype/offset/token hash検証。
- memory admission。
- corrupt block quarantine。
- 最初の出力前のEXACT fallback。
- capacity enforcement。

### 7.2 後方互換性

- `schemaVersion` は1のまま。追加キーだけで深いmergeが効く。
- 保存済みconfigに`pagedKVCache`がなければ無効。
- public OpenAI/Anthropic wire formatは変更しない。
- Coordinator↔Worker protocol versionは当面1のまま。capabilities/statsへの追加fieldだけにする。旧Worker/Coordinatorは未知fieldを無視できる。
- 既存`promptCache` directoryとindexは移動/削除しない。新backendは別rootに保存する。
- runtime update/rollbackで旧cacheを読まない。namespace不一致で不可視にし、保持世代数と全体容量の範囲でのみ清掃する。
- LM Studio providerは対象外。
- mlx-vlmは従来APCを使い、新paged backendへ切り替えない。

## 8. request lifecycle

### 8.1 lookup

1. Coordinatorの既存memory admissionを通る。
2. Workerでchat templateを適用し、ランタイム自身のtokenizerでtoken IDsを得る。
3. Adaptive soft-token経路ならraw paged backendを完全にskipする。
4. 既存RAM LRUを試す。
5. pagedが有効でmodel capabilityが`eligible`なら、連鎖hashを順に計算し、最長の連続したfull-block prefixを探す。
6. 復元予測バイトとmemory headroomを比較。入らなければ`restore_budget_rejected`のmiss。
7. blockを1つずつ検証・load・concatenate。中間の1 blockが欠けたらそこで打ち切り、「穴あきprefix」は作らない。
8. 復元後の全layer offsetとprefix token countを検証。
9. 最低1 tokenのuncached suffixを残し、`stream_generate`へ渡す。
10. paged miss/不適格なら既存snapshot、それもmissならcold EXACT。

### 8.2 生成とfallback

- 「paged cacheを使った」ことをrequest stateに保持。
- `stream_generate`から最初のmodel eventが得られるまではclient-visible deltaを確定しない。
- 復元済みcache経路がその間に失敗したら、関係blockをquarantineし、新規`make_prompt_cache`と全promptで一度だけ再実行する。
- 1 tokenでもmodel出力をclientに送った後は再実行しない。二重応答を避け、そのrequestは従来のerror/cancel契約で終了する。
- cacheと無関係のmodel/kernel errorは再実行しない。「paged復元を使用し、最初のmodel evaluationで失敗した」場合だけを広く救済する。

### 8.3 store

1. 生成完了または中断時、現行と同じく`cached_length == prompt + generated`を検証。
2. memory pressureによる中断はstoreしない。
3. 再利用できる上限は`prompt_length`。generated replyのみを先行して保存しない。
4. 既存block hashはskip。新規full blockだけを1つずつ書く。
5. 保存前後に容量上限を確認。新blockがquotaより大きい場合は書かず`block_too_large`を記録。
6. requestの応答正常性はstore成功に依存させない。store失敗は必ずmetrics/logに記録して応答自体は成功のままにする。

## 9. 観測性

`capabilities.promptCache.pagedKV`と`GET /api/v1/prompt-cache`へ以下を追加する。すべて追加fieldである。

```json
{
  "configured": true,
  "enabled": true,
  "eligible": true,
  "capability": "block",
  "layoutFingerprint": "...",
  "formatVersion": 1,
  "blockSize": 256,
  "diskBytes": 0,
  "diskBlocks": 0,
  "hits": 0,
  "misses": 0,
  "tokensRestored": 0,
  "stores": 0,
  "deduplicatedStores": 0,
  "restoreBudgetRejects": 0,
  "corruptBlocks": 0,
  "exactFallbacks": 0,
  "lastMissReason": null,
  "lastFallbackReason": null
}
```

request metricsの`cache_tier`に`paged_disk`を追加し、`shared_prefix_tokens`は実際に復元した連続prefixだけを記録する。`cached_tokens`と同じ値を別層で欠落させない。Worker event → API log → SQLite → status/UIのend-to-end testを必須とする。

GUIは「設定された」と「そのモデルで動作中」を分ける。

- 設定済み・対応: `Paged SSDキャッシュ: 有効`
- 設定済み・checkpointのみ: `このモデルは完全snapshotで再利用`
- 設定済み・非対応: `Paged KV非対応（通常生成は継続）`
- fallback発生: 理由と回数。モデル応答は不要にエラー扱いしない。

## 10. 実装phase

### Phase 0: 基準の固定と実機計測

目的: 「高速化した」と比較できるbaselineを作る。

作業:

- 実機のactive mlx-lm/mlx-vlm/MLX/Python版、model engine、cache layout、`cacheBudget`を記録。
- 小型dense modelと1つの実用モデルで、cold、線形継続、古いターン分岐、worker restart後のTTFT/prompt tok/s/RSS/MLX active/cache/SSD write bytesを取る。
- 現行の`promptCache.memoryBlocks=off`と`auto`を別々計測し、mlx-vlm既存APCの数値を混ぜない。
- 対象modelごとに3回以上取り、medianとピークを記録。

完了条件:

- 無効時のバイナリ/ソース実行経路を比較できるfixtureがある。
- 実機で現行cache tierが動いていることを`cached_tokens > 0`で確認。

### Phase 1: 設定、capability、hash、観測性（KV保存なし）

変更候補:

- `Coordinator/mlxbar/settings.py`: 追加schemaとstrict validation。
- `Coordinator/mlxbar/workers/supervisor.py`: feature env、per-worker disk quota、instance IDを渡す。
- `Workers/mlx_lm_worker/paged_cache/{types,capabilities,hashing,probe}.py`。
- `Workers/mlx_lm_worker/adapter.py`: load完了後に、有効時だけself-check。
- `Coordinator/mlxbar/api/management.py`: 追加stats。
- `Coordinator/mlxbar/cli.py`: `config set-paged-kv-cache`。
- Swift settings/menu: 実験的機能のmaster toggle、SSD toggle/size、対応状況、クリア操作。日英同時対応。

テスト:

- トグルOFFでnew package import 0回、directory作成0回、worker envの影響なし。
- 全supported/unsupported fake layout。
- 実mlx-lmの空`KVCache` / `QuantizedKVCache` / `RotatingKVCache` / `ArraysCache` / `CacheList`に対する重み不要のprobe script。
- runtime class nameが同じでstate tupleが変わった場合の非対応化。
- 未知settingの保存、旧configのdeep merge、旧GUI/CLI互換。

完了条件: KVの保存/復元をまだ行わず、モデルごとの利用可否がstatusに出る。

### Phase 2: mlx-lm dense cacheのSSD block reuse

変更候補:

- `paged_cache/extract.py`, `restore.py`, `disk.py`, `store.py`。
- `PromptCacheStore.fetch/store/stats/clear_disk`にoptional backendを統合。
- `MLXLMAdapter.stream`にcache-source stateとfirst-output-before-retry guardを追加。
- Worker RPCの型を変えず、既存stats dictへ追加。

初期対応:

- まずplain `KVCache`のみ。
- `QuantizedKVCache`はstate tuple/dtype/group/bitsの実ランタイム往復が通った後に同Phase内の第2段で有効化。
- `RotatingKVCache`, `ArraysCache`, `CacheList`, model-owned custom cacheは非対応。従来snapshot/checkpoint/coldを使う。

必須test:

- 0/1/255/256/257 token境界。最新tailが必ずprefillされること。
- 同一prefix、中間分岐、一番初のblock分岐、完全不一致。
- 中間file欠落/破損で、それ以降を接続しない。
- token digest衝突の人工注入でmetadata検証が拒否する。
- dtype/shape/layer count/state arity/runtime/model/templateが1つでも違うとmiss。
- 一時ファイル残留、ディスク満杯、permission error、partial write、index/metadata破損。
- 復元中のmemory pressure、生成開始直前のmemory pressure。
- 復元経路の最初のmodel evaluationで例外→クライアントに何も送らずEXACT再試行。
- 1 token送信後の例外は再試行しない。
- cancel/memory pressure中断のcache pairing検証。
- 複数replicaが同時に起動/保存/清掃しても、他workerのfileを削除しない。
- 上限を下げた後、使用中fileを破壊せずLRUで上限内へ収束。

実機受入基準:

- OFF時のcold/warm応答テキスト、finish reason、usage、tool call、cancel契約がbaselineと同一。
- ON時の同一seed warm-to-warmが再現する。cold-to-warmの極小数値差はbatch/kernel差を分離して記録し、テキスト品質の差を自動許容しない。
- 中間分岐で`cached_tokens`が最長連続prefixと一致。
- worker restart後もSSD hit。
- ピークRSS/MLX active+cacheがOFF時の安全上限を超えない。メモリ圧注入でcacheが機能より先に捨てられる。
- SSD使用量がquota + 最大1 blockの一時許容幅を超えず、prune後はquota以下。
- cache hitでTTFTが改善することを実数で確認。改善しないモデルは対応対象に入れない。

### Phase 3: exact terminal blockと対応拡大

条件: Phase 2が実機で安定し、partial tailの再計算が実用上のボトルネックだと数値で確認されること。

- terminal blockは通常連鎖とdomain separationし、完全一致時だけall-or-nothingで復元。
- `QuantizedKVCache`は実ランタイムと実モデルで往復、group/bits/dtype/offset検証が通った場合だけ対応。
- 再帰/rotating/compositeは引き続きcheckpoint-only。任意block連結はしない。

### Phase 4: 共有quotaとRAM hot tier

条件: Phase 2/3のSSD hitでもI/OがTTFTの支配項であり、RAM二重保持の利得が実機数値で上回ること。

- `memoryTier=auto`を実動。既定はそれ以降も`off`。
- Coordinatorが全workerのhot/disk quota leaseを所有。workerごの物理RAM比ではなく、全resident model合計が上限内になるようにする。
- hot tierはraw bytesまたは再構築可能な独立copyだけを持ち、live generation cacheとaliasさせない。
- pressure時はidle hot block → existing RAM prompt cache → idle modelの順に解放し、leased/live cacheに触らない。
- background writerを入れる場合は、未書き込みbytesのhard cap、queue full時のstore drop、shutdown flush timeout、MLX owner threadでの事前materialize/raw-copyを必須にする。

### Phase 5: Adaptive Memoryのhybrid KV reuse

条件:

- `softToken` 自体が27B級の実機で品質、TTFT、peak memory、電力のゲートを通る。
- `input_embeddings`経路でのcache save/load往復が実モデルで通る。
- writer/reader/template/policy/runtime/exact head/latent bandの完全keyが一致する。

実装:

- 既存`hybrid_key`を利用するが、format/layout/dtype/shape/soft-token countを追加してversion 2とする。
- raw exact paged storeとdirectory/namespace/stats/clear operationを分離。
- hitしてもshape/hidden dimension/offsetが1つでも不一致なら破棄し、そのrequest全体をraw EXACTへ戻す。soft-tokenの再計算だけで続行しない。
- 既存`persistentHybridKV` に加え`hybridKVReuse=false`を必須にし、旧configが突然ONにならないようにする。

## 11. テスト戦略

### 11.1 単体・property test

- 連鎖hashの決定性、parent sensitivity、token順序sensitivity、namespace/domain separation。
- 任意のtoken列と分岐位置に対して、復元prefixが必ず実際のcommon prefix以下である。
- store/load round-trip後のstate tree、dtype、shape、offset、meta_state一致。
- 不正file/path/symlink/header/metadataのfuzz。
- disk quotaとLRUのproperty test。削除失敗でinfinite loopしない。
- フォールト注入を全I/O境界に行い、generation resultを壊さない。

### 11.2 実ランタイム・重みなし

`scripts/verify-paged-cache-runtime.py`を新設し、各active/staged runtime slotのPythonで実行する。

- 各cache classのstate descriptor、tuple arity、trim/capture能力。
- 小さな実MLX配列を使ったslice/serialize/load/restore。
- `mx.save_safetensors`の実ファイル名、atomic rename、empty tensor、scalar/metaの実振る舞。
- runtime版違いの相互不読み。

fake MLXだけのtestを受入根拠にしない。

### 11.3 実モデルE2E

最低マトリクス:

| 経路 | model種 | 必須scenario |
|---|---|---|
| mlx-lm | plain dense KV | cold, RAM hit, SSD block hit, branch, restart, cancel, corruption |
| mlx-lm | quantized KV | defaultで非対応、対応候補probe/round-trip |
| mlx-lm | hybrid/recurrent | paged非対応と従来fallback |
| mlx-vlm | text-only VLM | 既存APC回帰 |
| mlx-vlm | image | 画像の異なるrequestがcache共有されない |
| adaptive memory | softToken OFF/ON | raw paged storeへ混入しない |
| model pool | 1/2 model, replicas 1/2 | quota、isolation、同時終了、unload |
| runtime update | old→new→rollback | 各slotのnamespace分離と旧経路回帰 |

### 11.4 長時間・クラッシュtest

- store中に`SIGKILL`、再起動後にpartialを無視し通常生成できる。
- restore中にclient disconnect/cancel-all/model unload。leaseとfile handleが残らない。
- disk full/read-only/permission change。生成は継続。
- 24時間の分岐ターン連続で、Python metadata、file count、SSD bytes、RSSが上限内へ収束。
- memory pressure warning/criticalを注入。使用中cacheを破壊せず、新規reuseが止まり、必要なら既存の503になる。

## 12. rolloutとrollback

### 12.1 rollout

1. Phase 1は保存なしでmerge可能。既定OFF。
2. Phase 2をdeveloper-only/CLIで有効化。GUIには「実験的」と明示。
3. 実機マトリクスと24時間test完了後に通常GUIの実験セクションへ公開。
4. 複数releaseで問題がなくても、自動ONにはしない。既定値変更は別の明示判断と全対応modelの実機証拠を必要とする。

### 12.2 即時rollback

- ユーザー: `experimental.pagedKVCache.enabled=false`。service再起動後に従来経路のみ。
- CLI: `mlxbarctl config set-paged-kv-cache --enabled false`。
- キャッシュデータ: 新規`clear-paged` endpoint/CLIでpaged rootだけを削除。既存snapshotやRAG/adaptive notesに触らない。
- runtime回復: 既存slot rollback。cache namespaceはslotごとに分離済み。
- 機能の自動circuit breaker: 同一worker/namespaceで3回連続の復元失敗または1回のlayout/offset不一致が発生したら、そのworker生存期のpaged read/writeを無効化。通常生成は継続し、statusに理由を出す。

## 13. 実装前に回答すべきgo/no-go

Phase 2へ進む前に、次をすべてYesにする。

- OFF経路がsource-levelとtest-levelで従来と同一か。
- 対応できないcache layoutを「静かに対応」としていないか。
- 復元先はruntimeが今作った新鮮なcache classか。
- 全layerのoffset/shape/dtypeとtoken prefixを一致確認したか。
- 一時メモリ、未書き込みbytes、SSD bytesのすべてにhard limitがあるか。
- MLX tensor操作がowner thread外へ出ていないか。
- 復元失敗で、何も出力する前にEXACTへ一度だけ戻れるか。
- 複数model/replicaでbudgetがN倍にならないか。
- runtime更新とrollbackでcache namespaceが混ざらないか。
- 実機で「動いている」ことを`cached_tokens`、TTFT、RSS、SSD bytesで確認したか。

1つでもNoならrelease対象にしない。

## 14. 優先順位

1. **Phase 0 + Phase 1**: ほぼリスクなく、将来のランタイム変更を検出できるため、単体でも価値が高い。
2. **Phase 2 plain KV only**: 実用的なbranch/restart高速化の中心。既定OFFの実験機能として導入。
3. **Phase 3 terminal/quantized**: 実機で必要と判明したものだけ。
4. **Phase 4 hot tier/shared quota**: SSD I/Oが数値でボトルネックと証明されてから。
5. **Phase 5 soft-token hybrid KV**: soft-token自体の品質評価完了後の別案件。

continuous batching、oMLX scheduler全体の移植、VLMの独自cache置換、KV quantizationの同時導入は、この計画に混ぜない。

## 15. 参照

- [jundot/omlx README](https://github.com/jundot/omlx/blob/b390b31e0c6831225fed0f24d278eb1db7fcb68b/README.md)
- [oMLX paged cache](https://github.com/jundot/omlx/blob/b390b31e0c6831225fed0f24d278eb1db7fcb68b/omlx/cache/paged_cache.py)
- [oMLX block-aware prefix cache](https://github.com/jundot/omlx/blob/b390b31e0c6831225fed0f24d278eb1db7fcb68b/omlx/cache/prefix_cache.py)
- [oMLX SSD cache](https://github.com/jundot/omlx/blob/b390b31e0c6831225fed0f24d278eb1db7fcb68b/omlx/cache/paged_ssd_cache.py)
- [oMLX cache type handlers](https://github.com/jundot/omlx/blob/b390b31e0c6831225fed0f24d278eb1db7fcb68b/omlx/cache/type_handlers.py)
- [mlx-lm cache implementation](https://github.com/ml-explore/mlx-lm/blob/e5962529e5614ce00f14bdd39fe5fc6e410ca2b0/mlx_lm/models/cache.py)
- [mlx-vlm APC implementation](https://github.com/Blaizzy/mlx-vlm/blob/cdc745ad8a32d162f6d8e9d08be256910d663ac2/mlx_vlm/apc.py)
- [oMLX issue #1925: TurboQuant/hybrid type mismatch](https://github.com/jundot/omlx/issues/1925)
- [oMLX issue #2227: trailing partial block](https://github.com/jundot/omlx/issues/2227)
- [oMLX issue #2255: restored KV corruption symptoms](https://github.com/jundot/omlx/issues/2255)
- [oMLX issue #95: concurrent MLX/Metal operations](https://github.com/jundot/omlx/issues/95)
- [oMLX issue #2064: disk quota overrun](https://github.com/jundot/omlx/issues/2064)

## 16. 実装結果（2026-09-13）

### 16.1 完了した範囲

- `experimental.pagedKVCache`を追加。master switch、SSD、容量、branch reuseを個別設定でき、既定は無効。
- OFF時はpaged packageをimportせず、probe・環境変数・directory作成を行わない。
- `Workers/mlx_lm_worker/paged_cache/`へcapability、連鎖hash、immutable safetensors block、復元、quota、統計を独立実装。
- 256-token full blockだけを保存・復元し、常に1 token以上のuncached tailを残す。
- mlx-lm 0.31系のstate arity 2と新runtimeのarity 3を実MLX setter probeで判定し、namespaceへ含める。
- plain `KVCache`以外はfail-closed。量子化、rotating、composite/recurrentは`checkpoint_only`または`unsupported`。
- block本体と0600 checksum sidecarを同一directoryで一時作成・fsync・atomic rename。読込前にストリーミングSHA-256検証し、破損blockは隔離する。
- tensor/checksumのrename間のクラッシュで生じるhalf-pairは、起動時に加えて次回store時にも検出・削除し、再起動なしで再生成する。孤立checksumも起動時に削除する。
- モデルconfigから算出したbytes/tokenと実block bytes、全layerのshape/dtype/offset/state arityを照合する。
- 復元時はlive result + concatenate scratch + 1 block、保存時は1 contiguous blockを事前計上し、上限またはMLX active memoryが不明/不足ならmiss/skipにする。
- 期待ファイルサイズ上限を`mx.load`前に確認し、余分なtensor keyも拒否。cache directoryのsymlinkを拒否する。
- 最初のmodel event前のpaged復元起因失敗だけfresh cacheでEXACT再試行。1 event後は再試行しない。
- 同一worker/namespaceで3回連続の初期評価/復元失敗、または1回のlayout/offset mismatchでcircuitを開く。
- workerごとのprivate rootへ保存し、設定上限を`maxResidentModels`で割って複数model/replicaによるN倍化を防ぐ。旧namespace、隔離block、checksum、exact markerもquotaに含める。
- `GET /api/v1/prompt-cache`、GUI、CLIへcapability・bytes・blocks・hit/miss・restored tokens・fallback/corruptionを追加。Pagedだけを消すAPI/GUI/CLIを追加。
- Adaptive Memoryへ`hybridKVReuse: false`を追加。既存`persistentHybridKV: true`だけでは将来も自動で有効にならない。

### 16.2 検証結果

- Python全回帰: 固定順492件、ランダム順492件（安全性レビュー後の最終構成）成功。
- Swift debug/release build成功、Localizable.stringsの`plutil -lint`成功、`git diff --check`成功。
- 実mlx-lm slot（mlx-lm 0.31.3 / MLX 0.32.2）で、real MLX arrayのslice → safetensors → checksum → load → concatenate → fresh cache restoreを確認。
- 同slotでstate arity 2、2 block/512 tokens、途中分岐256 tokens、別namespace miss、一時ファイルなしを確認。
- 同slotでhalf-renamed blockの再起動なし自己修復、空きメモリ不足時のstore skipを実MLX arrayで確認。
- 隔離Coordinatorで設定の取得/変更、Paged専用clear、再起動後の設定永続化を確認。パッケージ再構築・署名検証も成功。
- 実モデル`Ornith-1.5-9B-MLX-8bit`は`non_plain_kv_layout` / `checkpoint_only`と判定し、Paged directory/storageへ進まないことを確認。

### 16.3 意図的に未実装の範囲

Phase 3のterminal partial block/quantized KV、Phase 4のRAM hot tier/Coordinator動的quota lease/background writer、Phase 5のsoft-token hybrid KVは、本文の実機受入条件を満たす証拠がないため実装していない。設定したのに黙って動かない状態を避けるため、`memoryTier`は現版で`off`以外をvalidation errorとする。これは未完了ではなく、本設計の安全ゲートを適用した結果である。
