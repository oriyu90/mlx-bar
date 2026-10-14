# DESIGN v2.7.0 — 生成後の待機表示の自己回復 / レプリカ集約表示 / 同一モデルのオンデマンド並列 / 非固定モデル待機時間の可視化

- 版数: app 2.7.0 / build 47。Coordinator `__version__` 2.7.0。`schemaVersion` は 1 のまま（追加のみ）。
- 方針: 既定値不変、追加のみ、検証失敗は fail-closed、適用不可は queue＋理由報告。Coordinator が正本、GUI/CLI は薄いクライアント。

## 1. 背景（オーナー報告の3件）

1. モデルが応答を生成したあとも、アプリ側の行表示が待機中（Idle）に戻らないことがある。
2. 生成並列数（`generationConcurrency`）を設定しているのに、2つ目の並列生成が常駐モデルの制限で使えないことがある。
3. 非常駐モデルがロードされたときの、アンロードまでの時間を設定できるようにしてほしい。

調査の結果、(3) の設定自体（`models.pool.idleTTLSeconds`、既定900秒、範囲30〜86,400秒、GUI＋CLI対応済み、再起動不要）は既に存在した。v2.7.0 では設定の存在を可視化（行の残り時間・`model resident` の待機時間と解放予定）で応え、新規キーや既定値変更は行わない。(1)(2) は実バグとして修正する。

## 2. 修正内容

### 2.1 生成後の待機表示に戻らない（3層の修正）

原因は3つあり、いずれも「生成中」を示すカウンタが残留することだった。

**(a) 切断時の `aclose` 未達による lane/lease 残留（Coordinator）。**
`generate_for_model` は lane と lease を自身の `finally` で解放するが、その `finally` は API 層が
async generator を `aclose()` して初めて走る。素の `await generation.aclose()` は、クライアント切断の
巻き戻し中（`CancelledError`/`GeneratorExit` 処理中）にそれ自体がキャンセルされ、`finally` 内の
後続行（lane 解放・lease 減算）まで到達しないことがあった。残留した `activeLeases > 0` を行判定
（`activeLeases > 0 → generating`）が拾い続け、永久に「応答生成中」になった。

- 対策1: 全7箇所の `aclose`（`openai_compat.py` 4、`anthropic_compat.py` 2、`management.py` 1、
  `context_compression.py` 1）を新設ヘルパー `_aclose_generation` 経由の shield 付き close へ統一。
  クリーンアップの失敗は握りつぶし、要求の成否に波及させない。
- 対策2: `model_pool.generate_for_model` と `supervisor.generate` の queue 待機 `finally` を、
  同期状態の解放（queue 除去・lane 解放・lease 減算・`last_released_at` 更新）を先、
  ヘルパータスクの cancel＋join を後（shield＋suppress）へ並べ替え。join の成否が状態に波及しない。
- 対策3: `status()` が各スロットに `_recover_slot_state(slot, "status")` を適用する。
  従来の `_recover_lane` は次回要求時（`new_request`/`queue_wait`/`capacity_check`）にしか走らず、
  idle モデルは次回要求が来ないため残留が永久化した。`status()` はメニューバーが毎秒 poll するため、
  残留の上限は1ポーリング周期になる。lease 回復は「worker active なし・queue なし・owner なし・
  lane lock 解放」の4条件が揃ったときのみ行う。入口経路（`leases += 1` から lane 確保まで）は
  その4条件がすべて空の区間に `await` を含まないため、イベントループがその状態の live 要求を
  `status()` に観測させることはなく、誤回復はない（§4 のテストで固定）。

**(b) レプリカ集約欠落による行表示の固定（Swift）。**
`MenuBarViewModel.refreshStatus` は同一モデルIDを先頭1件に潰し、`generationTokensPerSecond` だけを
max 集約していた。`activeLeases`/`laneQueueDepth` は先頭レプリカの値のままだったため、生成中の
replica 1 が隠れる／残留 lease 持ちの replica 0 が行を「生成中」に固定する、の両方向の誤表示があった。

- 対策: 同一IDの辞書を先にマージしてから `ResidentModel` 化する。`activeLeases`・`laneQueueDepth` は合計、
  rate は max、replica 数は max、メモリ予約は合計、`keepLoaded`・外部管理は OR、`poolState` は
  loading > evicting > external > ready の優先度、 `idleExpiresAt` は最も早い expiry。
  既存の単一レプリカ構成では合計＝そのままなので表示は v2.6.0 と同一。

### 2.2 2つ目の並列生成が使えない（オンデマンドレプリカ拡張）

`maxResidentModels` は生成経路を直接拒否しない（`generate_for_model` は `_admit` を呼ばない）。
v2.0.1 以降、同一モデルの追加レプリカも distinct カウントで吸収されるため、`maxResidentModels = 1`
でも同一モデルの2体目はロードできる。実際の詰まりは別にあった：API 自動ロード（`pin=False`）は
常に1コピーだけ立ち上げ、2体目は pin 済みプロファイルの背景補充（`_scale_up_pinned_replicas`）にしか
任されなかった。つまり `generationConcurrency = 2` だけでは同一モデルの2並列は常に直列キューに
落ち、ユーザーの体感は「常駐制限で2つ目が使えない」だった。設定UIの説明文（「同一モデルへの要求は
到着順に直列化」）も v1.8.0 のレプリカ対応後に追従しておらず、誤解を助長した。

- 対策: `generate_for_model` が busy なレプリカを拾ったとき（＝ready 全体が busy。idle があれば
  `_pick_replica` がそちらを返すため）、`_gen_concurrency > 1` かつ replica 数が
  `min(maxReplicasPerModel, _gen_concurrency, 8)` 未満なら、`_maybe_expand_replica` で1つだけ
  `pin=False` のオンデマンドレプリカをベストエフォートで追加し、再 pick して使う。
  - admission は `_load_replica → _admit` の既存経路（per-model 上限・全体予算・OS 圧力・空き余白）を
    そのまま使い、拒否時は busy スロットへの queue（＝v2.6.0 の挙動）へフォールバックする。
    失敗は `replicaShortfalls` に記録し、GUI/CLI の既存表示に出る。
  - lane 開始時の `_admit_concurrent`（ヘッドルーム＋圧力）も効いたまま。2レーン目以降の安全ゲートは
    一切弱めていない。
  - `pin=False` のため、idle になった拡張分は reaper の TTL とレプリカ縮退で回収される。
    バースト並列の一時容量であり、常駐設定を書き換えない。
  - `generationConcurrency = 1`・`maxReplicasPerModel = 1` では拡張しない（従来の直列と同一）。
- UI/文書: 設定画面に加筆（同一モデルの同時要求は空きレプリカ優先、なければ自動追加、
  `maxResidentModels` は異なるモデルの種類数、の2行。既存文言は不変で加筆のみ、日英）。
  README の3箇所の直列記述を v2.7.0 の挙動へ更新。`settings.py` の `generationConcurrency`
  コメントの「同一モデルは常に直列」を現行へ更新。

### 2.3 非固定モデルの待機時間を設定・可視化

`idleTTLSeconds`（既定900、範囲30〜86,400）は既存のまま。新規キー・既定値変更なし。

- `modelPool` status に `idleTTLSeconds` を追加（設定往復なしで表示できるよう。増分フィールド）。
- `mlxbarctl model resident` に `idleTTLSeconds` と行ごとの `idleExpiresAt` を追加（増分）。
- GUI: 待機中かつ非固定かつ外部管理外の行に残り時間（90秒未満は秒、以上は分、日英インライン文言。
  既存 `activityText` と同一流儀のため `.strings` 追加なし）を表示。設定画面に2行加筆（自動追加と
  TTL 即時反映、日英 `.strings` へ2キー追加）。
- TTL 変更は reaper が毎周期 `_pool_settings()` を読むため再起動不要（従来どおり）。設定画面の
  既存注意文（再起動対象の列挙）は触らず、TTL 即時反映の1行を別 Text として追加した。

## 3. 互換性・安全性の不変条件

- 設定スキーマ追加なし（`DEFAULTS` 不変、`schemaVersion` 1）。既存 `config.json` はそのまま読める。
- `replicas = 1` かつ単一モデル通常運用の表示・挙動は v2.6.0 と同一（集約の合計＝単一値、拡張は
  busy 時のみ発火、TTL フィールドは増分）。
- メモリ安全性：拡張は `_admit` の全ゲートを通し、lane 開始は `_admit_concurrent` の全ゲートを通す。
  拒否時は queue（失敗させない従来則）。`_reap_once`・`_scale_up`・退避抑止（`leases > 0`）は無変更。
- クラッシュ安全性：`_recover_slot_state` は同期のみ（`status()` は sync）。`_aclose_generation` と
  並べ替えた `finally` は `BaseException` を suppress し、要求の成否に波及させない。
  `GeneratorExit` 中の `yield` は行わない。
- 並行安全性：拡張の同時二重起動は `_load_replica` の singleflight（replica 単位の future 共有）が
  吸収する。`status()` からの回復は上記4条件でのみ発火し、live 要求の観測不能性は入口経路の
  await 非存在で保証する（テスト `test_status_leaves_a_live_generation_alone` で固定）。
- UI：新規 `.strings` 重複なし（既存 `モデル未ロード` 重複1件は残置）。`.fixedSize` 新規使用なし。
  設定画面の長文化は既存 `ScrollView` 構造の内側（v2.5.1 確立パターン）。

## 4. 検証（`TEST_PLAN_v2.7.0.md` に記録）

- 新規 `Tests/test_v2_7_parallel_idle.py` 6件：残留回復、live 非破壊、拡張（`maxResident=1` でも
  2並列）、メモリ拒否フォールバック、上限1での queue 維持、TTL 可視化と live 反映。
- 既存 `Tests/test_model_pool.py` 2件は意図的変更に合わせ設定を明示化（`maxReplicasPerModel=1` で
  直列・cancel 維持。v2.0.0rc1 の前例に準拠）。`concurrency=1` 系と `test_model_replicas.py` 全件は
  無変更で緑。
- 既知 fluke `test_ttl_starts_when_a_slow_load_finishes` はクリーンツリーでも失敗を確認（本版の回帰ではない）。
- `swift build` 成功、日英 `.strings` lint OK。
