# DESIGN v2.8.0 — 非常駐モデルと待機時間の延長、および生成中断の協調化

2026-10-18。オーナー依頼2件に対する設計と実装の記録。

1. **常駐上限の超過ロード** — `models.pool.maxResidentModels` 個のモデルを
   常駐でロード済みの状態でも、それ以外のモデルをロードできるようにする。
   ロードされたモデルは常駐に数えず、非常駐モデルは待機有効時間が過ぎれば
   アンロードする。GUI・CLI の両方からその時間を `+N秒` 延長できるようにし、
   アンロードまでの時間を秒刻みで表示する。GUI は「ロードされているモデルが
   一覧として出てくる最初の画面」（メニューバーのパネル）に、モデルの下に
   入力欄を置く。
2. **CPU 消費問題の調査報告書の検証と反映** — 添付された
   `MLXBar_CPU問題_報告書.md`（v2.7.4 実測）に対する検証と、必要な修正。

---

## 1. 非常駐モデル（上限を超えたロード）

### 1.1 何を変更したか

v2.7.3 は「上限未満は追加、上限到達は `RESIDENT_LIMIT_REACHED` (409) で
明示エラー」にした。

上限の枠が埋まっている状態で別のモデルをロードしようとして弾かれる、
という判断自体は正しかった。「黙って追い出さない」という方針は本章でも
保つ。ただしオーナーは、上限に到達しているときにも他のモデルが
ロードできることを明確に求めていた。つまり**「弾く」のではなく
「枠を消費しない第2の tier としてロードする」**が要求である。

v2.8.0 は、上限が resident の**枠数**であって**ロードの可否**ではないことに
する。

| 状態 | v2.7.3 | v2.8.0 |
|---|---|---|
| 常駐数 < 上限 | 新規 resident としてロード | 同じ |
| 常駐数 = 上限、同一モデル | ロード（replica 含む） | 同じ |
| 常駐数 = 上限、別モデル | `RESIDENT_LIMIT_REACHED` (409) | **非常駐としてロード** |
| メモリ安全でない | 各 code で拒否 | 同じ（**変更なし**） |

### 1.2 「非常駐」の定義

`PoolSlot.resident: bool`（既定 True）。

- `resident` は**admission の瞬間に一度だけ**決まる。`_load_replica` が
  `_pool_lock` を保持したまま判定するので、並行ロードで枠を二重に数えない。
- 同一モデルは最初の replica の値を継承する。`maxResidentModels` は
  「異なるモデルの種類数」の上限であり、v2.0.1 以降ずっとその意味なので
  replica は別として数えない。
- **非常駐は `session_pinned` にならない。** 固定が resident になる仕組み
  であり、非 resident にする「理由。だから `pin=True`（load API の既定値）を
  渡しても非常駐は固定されない。**これが「待機時間を過ぎたらアンロードされる」
  という要求を満たす条件そのもの**。
- `keepLoaded` プロファイルは**尊重する**（常駐枠が縮むことはない）。
  オーナーが明示的に設定した常駐の希望なので、上書きして解放するのは
  「黙って追い出す」に近くなる。

### 1.3 非常駐は「無効」ではない

ここが重要。`resident: false` は「`maxResidentModels` の枠を消費しない」
という意味であり、**「使用できない」ではない**。

- worker process は起動している
- `generate_for_model` はそのモデルを選ぶ（`_pick_replica` は resident に
  関係なく ready を見る）
- API は要求されたモデルを返す（v2.7.2 の厳格解決が維持される）

`_admit` のメモリチェック（per-model 上限・全体予算・OS 圧力・空きメモリ）は
`resident` の判定より**前**に走り、非常駐でも全く同じ条件で拒否される。
`MODEL_MEMORY_LIMIT` / `MEMORY_BUDGET_EXCEEDED` / `MEMORY_PRESSURE` は
`Tests/test_v2_8_overflow_idle.py` で非常駐ロード中も検証する。

### 1.4 どのモデルが resident か（再計算しない）

一度 resident になれば、unload されるまで resident のまま。空き枠が
生まれても既存の非常駐を resident に「昇格」させない。理由：

- 昇格すると、UI の「非常駐」バッジの消滅と「枠が1つ増えた」の表示が
  1 秒ごとの status ポーリングで振動する。
- 昇格しても挙動は変わらない（TTL は resident でも非常駐でも同じに効く）。
  違いは表示だけなので、実装する価値がない。

**明示的な pin のみ**昇格のきっかけになる（`_pin_slot` → `_promote_if_room`）。
「このモデルは残しておきたい」は resident 枠を消費する意思表示として扱う。
枠が満杯なら昇格しない（`maxResidentModels` は hard cap）。この場合
session pin は付くので TTL からは守られるが、resident 枠は消費しない。

### 1.5 reaper の変更

- 期限判定を `_idle_remaining()` に統一。status が表示する数値と、実際に
  解放される数値が同じ式から出る（`_reap_once` と `status()` の二重定義を
  やめた）。
- 予算逼迫時の victim 順序を `(resident, last_released_at)` に。
  **非常駐が先**。非常駐は「pool の予備容量」なので、そこを先に回収する
  ほうが resident を持つモデルを壊さない。
- `_resident_models_after()` は resident のみを数える。非常駐は枠を
  消費しないので、上限引き下げ時に解放すべきなのは resident 側。
  resident 枠が埋まっている場合、非常駐を解放しても枠は空かないので
  ループで飛ばす（`if not over_budget and not slot.resident: continue`）。

### 1.6 status への追加

`loadedModels[]`:
- `resident: bool`
- `idleSecondsRemaining: int | null` — 解放までの残り秒。固定・keepLoaded・
  external では null。
- `idleExtendSeconds: float | null`

`modelPool`:
- `residentModelCount` — **resident のみ**の distinct 数（v2.7.3 までは
  全ロード数を数えていた。名前と意味を揃える）。
- `nonResidentModelCount` — 追加。
- `residentCount` は従来どおり「ロード済みエントリ数」（pool の占有量）。
  意味を変えていない。

### 1.7 待機時間の延長 API / CLI

`POST /api/v1/models/{id}/idle/extend` body `{"seconds": N}`

- 1〜86400 秒。範囲外・非数値は 422 `INVALID_IDLE_EXTENSION`。
- 未ロード、または固定／keepLoaded で TTL を持たないモデルは 409
  `MODEL_NOT_IDLE_MANAGED`。「固定中のモデルには待機時間がないため延長
  できません。固定を解除してから再度延長してください」。
- 加算。`_idle_remaining()` を読むので status の表示と必ず一致。

**one-shot であることの根拠**: 延長値は `slot.idle_extend_seconds` に格納され、
`generate_for_model` の `finally`（= そのモデルが次に使われた瞬間）に
`0.0` に戻る。繰り返し延長しても恒久 pin にはならない。「もう少し
持たせる」が「永久に持たせる」に化けられない。

CLI: `mlxbarctl model extend-idle <model_id> <seconds>`。
`model resident` の出力にも `resident` / `idleSecondsRemaining` /
`idleExtendSeconds` / `nonResidentModelCount` を追加（従来は明示的な射影
リストで、書かないと出力に出なかった）。

### 1.8 GUI

- 一覧ヘッダに `ロード済みモデル · N（常駐 X・非常駐 Y）`。
- 行に「非常駐」バッジ。
- **解放までの時間を秒刻みで表示**。v2.7.0 の「90秒以上なら分」丸めは
  削除。理由は2つ：オーナーの明示要求であること、そして丸め切り替えで残り
  時間が「止まったように見える」こと。秒刻みなら式が1本で、延長 UI が
  足すべき対象も同じ数字になる。
- 入力欄は `IdleExtendField`（新規ファイル）として行の下に配置。
  `resident.isIdleManaged` のときだけ表示する（固定モデルには期限が無いので、
  押したら 409 になるだけの UI は出さない）。
- `@State` はビュー側に置き、`Binding(get:set:)` は使わない。status は
  1 秒ごとに再構築されるため、setter で発火する binding は毎回再送信して
  しまう。送信は Return キーかボタン。
- レプリカ集約（`refreshStatus` の merge）:
  - `resident` = **AND**（resident が1つでもあれば行は resident）。
  - `idleSecondsRemaining` = **min**（最も早い期限しか約束できない）。
  - `idleExtendSeconds` はどれか1つでもあれば採用。

### 1.9 互換性

- API のレスポンス形式・ステータスコードは、**新しい項目が増えただけ**。
  既存クライアントは無視する。
- 破壊的変更は v2.7.3 の 409 のみ。これはオーナーの明示依頼そのもの。
- OpenAI/Anthropic 互換 API の意味は変わらない。むしろ `model-b` が 409 で
  なく正しく応答するようになる。
- 新規設定キー・既定値変更なし。

---

## 2. CPU 消費問題の調査結果

報告書の主張を、ソースと実測の両方で検証した。**全件が実在したわけではない**。
採用したものと採用しなかったものを分けて記す。

### 2.1 実在したもの → 修正

**P0（§2.2 / §5.1）: 中断が「キューに積むだけ」**

`close_on_mlx_thread()` は `mlx_executor.submit()` であり、single-worker の
executor の**末尾**に積まれる。実行中の `next_event()` が完了するまで
`close()` は動かない。報告書が引用しているソースコメント
（`"occupies the single MLX thread that every later request needs"`）は
コードの意図と一致していた。

→ **修正**: `GenerationAbort`（`threading.Event` ベースの thread-safe handle）。
`next_event()` の**`next(iterator)` の直前**で検査する。ここでなければ
MLX 処理がすでに始まっている。`/rpc cancel` も handle に signal を送る。
切断時は `lines()` の `finally` の冒頭（await を書いても取り消しが
巻き戻って飛んでいく危険のない位置）で set する。

これによる API 互換性はゼロ：レスポンス形式・ステータスコード・SSE
フォーマットすべて不変。変わるのは生成が早く打ち切られることだけ。

**新しい backstop: orphan limit**

`tokenIdleTimeoutSeconds` は「沈黙した worker」を捕まえる。「heartbeat と
token を出し続けるが consumer がいない」worker は捕まえない。これは切断が
検出されていない接続（proxy、TCP half-open、SSE proxy の timeout 使い回し）
での実在する形状で、`max_tokens` まで生成し続ける。

→ `ORPHAN_GENERATION_SECONDS`（既定 4 時間）を追加。ただし **coordinator の
`totalTimeoutSeconds` を渡す**ので、これが唯一の正となる。worker 側の
fallback はそれを undercut してはならない（ユーザーが1時間許可した生成を
勝手に30分で切ることはない）。範囲外の値は clamp ではなく ignore
（不正な config が早期 cut-off にしてはならない）。

**P2（§5.4）: rayon / tokenizers**

報告書の `sample` で `tokenizers.abi3.so` が 174 サンプルと歴代首位。
Rayon はコア数からプールを作るので、28 コア機では worker（本来は1スレッド
直列化）の中に 28 本の tokeniser スレッドが立つ。

→ `mlx_lm_worker/adapter.py` の import 前に `RAYON_NUM_THREADS`
（既定 `min(4, cpu_count)`）と `TOKENIZERS_PARALLELISM=false` を
`setdefault`。tokenizers が読む前に設定する必要がある。rayon のプール数は
並列度を変えるだけで token 列自体は変えないので、prompt cache の正しさ
（キャッシュ可否は token id 列で決まる）には影響しない。

### 2.2 実在しなかったもの → 不採用

**「`max_workers=1` が問題」**

報告書は「単一 MLX スレッド」と題しているが、これは**設計通り**。MLX は
GPU stream 状態をスレッドごとに持つので、MLX に触れる操作を1本の専用
スレッドに直列化しないと `There is no Stream(...) in current thread` で
落ちる。報告書も本文で「不可避の正しい設計」と認めている。スロット数
（`maxResidentModels`、`maxReplicasPerModel`）を増やすのが正しい対策だが、
オーナーの依頼は別の所（枠を超えたロード）なので、本版では扱わない。

**「`close()` が `next_event` の完了まで待たれるので飢餓する」→ 実測で否定**

この主張を計測した。MLX スレッドを空けた状態で切断した場合、
**切断後に生成された token は 0** だった（旧コード・新コードとも）。
`close()` は in-flight `next_event()` の直後に走り、generator はそこで
`GeneratorExit` で抜ける。実測値：

```
MLX executor が空いている切断:      OLD   0 token / NEW   0 token
1 トークン 2 秒の切断:              OLD   0 token / NEW   0 token
MLX executor が 3 秒占有された切断:  OLD 14239 / NEW 14239  ← 後述
```

つまり「1 token の分だけ遅延する」という報告書の**記述**は正しいが、
「`max_tokens` まで生成が続く」という**帰結**は、少なくとも
executor が空いている限り出ない。

**ただし「executor が塞がっている場合」は報告書の核心であり、我々の修正が
効く想定の部分**。上表3行目がまさにそれで、executor が塞がれている間
`close()` は走れず、生成はその間も続く。協力中断は executor を介さないので、
塞がったままでも次の token を要求しない。

ただし実測では OLD/NEW ともに同じ（14239）だった。理由は harness 側の
不完全（executor を塞ぐ仕組みが generator 自体まで直列化していた）。
**この1行の測定は信頼できないので採用しない。**協力中断が効く範囲は、
設計上は「executor を介さない」で確定するが、実測による定量的裏付けは
**ない**。TEST_PLAN_v2.8.0.md の実機項目に残す。

**「§5.2 15分のソフトタイムアウト」→ 不採用**

`totalTimeoutSeconds`（既定 3600 秒）は**すでに機能している**
（`supervisor.py:614` の `asyncio.timeout`）。これに 15 分のソフト
タイムアウトを追加すると、素直に 20 分かかる生成が偽陽性で切れる。
報告書自身が「実用上は他の希望より先にチェックされる」と述べているが、
それは「追加しなければならない」という意味ではない。orphan limit を加えた
のは、この既存タイムアウトを**壊さずに**、かつ「consumer が居ない」場合
だけ効く layer を足すためである。

**「§6.1 maxResidentModels 3→1」→ 実装しない**

これは報告者が使っている機械の**設定**であり、コードにはしない。かつ
オーナーは明示的に「x個までで、x個ロード済みでもそれ以外をロードできる
ように」と依頼している。3→1 にすれば依頼と逆になる。**README の手順として
のみ記録する**（§3）。

**「§5.3 Metal ResidencySets の最適化」→ 不採用（計測していない）**

報告書の指摘は実在する（`sample` の `IOGPUResourceGroupUpdateResources`）。
しかしこれは MLX 内部の allocator の挙動で、MLXBar 側から安全に干渉する
API がない。KV キャッシュのバッファプール化は大規模で、PromptCacheStore の
再設計を要求し、本版のスコープを大幅にはみ出す。**計測もされていない**
（どの residency operation がどれだけ効いているか）。次版の実測課題として
記録する。

---

## 3. ユーザー向け設定（非推奨だが記す）

報告書の §6.1 は「同時保持モデルを減らす」名作だが、本版の非常駐機能が
あれば、**`maxResidentModels` を下げなくてもメモリ圧迫は自動的に緩和
される**。非常駐モデルは：

- `idleTTLSeconds`（既定 900 秒）経過で自動解放される
- v2.7.4 の queue＋pressure 即時解放が非常駐にも効く
- GUI/CLI から個別に延長できる

したがって owner の 40GB 問題は、`maxResidentModels: 3` を1に下げずに
「使用しないモデルをアンロードする」運用で解決できる。README に書く。

---

## 4. テスト

| ファイル | 件数 | 内容 |
|---|---|---|
| `Tests/test_v2_8_overflow_idle.py` | 29 | resident/非常駐の admission、非 resident の使用可、メモリ安全の非 resident への適用、reaper の順序・降格、延長の加算/消費/範囲、HTTP 経路、CLI wire |
| `Tests/test_v2_8_generation_abort.py` | 13 | 切断・`/rpc cancel` での即停止、executor 非経由、truncate が `stop` にならない、orphan limit、timeout 受け渡し、rayon bound |
| 既存 | — | v2.7.3 の suite は「拒否」から「非常駐」契約へ意図的に書き換えた（docstring に明記）。件数は維持。 |

**旧コードで落ちる**ことを確認した（`git stash` して新テストを旧実装に対して
実行し、`ORPHAN_GENERATION_SECONDS` を shim して collection を通したうえで）。
落ちるのは cancel の即時性・orphan limit・abort handle の3系統。

### テスト手段の注意

`TestClient` は httpx の ASGI transport がレスポンス body を buffer して
から返すので、**「クライアントが切断する」という事象を表現できない**。
今回の不具合は切断でしか起きないため、`test_v2_8_generation_abort.py` は
ASGI app を直接駆動して `http.disconnect` を配送する（`_GenerationRun`）。

---

## 5. 引き継ぎ

1. **協調中断の定量的効果は実機未測定。** executor が塞がった場合の差を
   計測できていない（§2.2 末尾）。TEST_PLAN_v2.8.0.md の実機項目。
2. **Metal Residency（§5.3）は未着手。** どの最適化が効くかを
   `sample` で計測してから。
3. **`RESIDENT_LIMIT_REACHED` は消えた。** v2.7.3 で追加され、v2.8.0 で
   削除。README・CHANGELOG・release notes に明記済。この code を分岐して
   いる外部スクリプトがあれば追随が必要。
4. **GUI の秒表示・入力欄は目視未確認。** build と strings lint のみ。
   英語 UI も同様（v2.5.1 からの継続事項）。
5. **`RAYON_NUM_THREADS` の 4 という値は実測未調整。** prompt cache の
   tokenize 速度が低下しないことを実機で確認してから確定する。
6. 既知の fluke `test_ttl_starts_when_a_slow_load_finishes` は継続。