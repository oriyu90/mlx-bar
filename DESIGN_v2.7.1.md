# DESIGN v2.7.1 — 常駐固定のライフサイクル修正 / 最新ランタイム互換の確定

- 版数: app 2.7.1 / build 48。Coordinator `__version__` 2.7.1。`schemaVersion` は 1 のまま（追加のみ）。
- 方針: 既定値不変、追加のみ、検証失敗は fail-closed。Coordinator が正本、GUI/CLI は薄いクライアント。
- v2.7.0 の挙動・表示・設定は不変。Swift の変更なし（サーバ互換の既定維持）。

## 1. 背景（オーナー報告の2件）

1. 非固定モデルが、固定モデルを差し置いて誤って固定されてしまう。
2. 最新の mlx-vlm などの導入に不具合が生じる。

調査の結果、(1) は単一の誤りではなく、固定（`profiles[].keepLoaded`＋セッション固定）と
非固定（API 自動ロードの transient）の区別が、再起動・ランタイム切替・固定解除の3経路で
崩れることだった。(2) は `mlx-vlm 0.7.8` / `mlx-lm 0.32.0` の表層 API（`load` /
`stream_generate` / `set_memory_limit` / プロンプトキャッシュ・APC の kwarg・応答字段）は
互換であることを実機スモークで確認し、残る実害（tool 呼び出しのネイティブ経路の陳腐化）と
導入時検査の不足（要求時に使う import を stage 検査が確認しない）を修正する。

## 2. 修正内容

### 2.1 再起動プリロードの固定化（`main.py`）

`_preload_last_model` は固定プロファイルと最終使用モデルを一つの `ids` に束ね、
すべて `load()` 既定（`pin=True`）で復元していた。API 自動ロード（`pin=False`）の
非固定モデルが再起動後にセッション固定へ昇格し、TTL 対象から外れ、固定枠を塞いだ。

- 対策: 固定プロファイルは `pin=True`、最終使用モデルは `pin=False` の二系統に分離。
  最終使用モデルが固定プロファイルと同一なら二重ロードしない（従来の `not in ids` 維持）。
  いずれも失敗は1モデルで打ち切らず次へ進む（従来則）。

### 2.2 ランタイム切替リロードの順序と中断（`model_pool.py` / `runtimes/service.py`）

`reload_resident` は snapshot の格納順（モデル ID 整列）のまま `pin` を復元し、
1件の失敗で残り全部を中断した。`maxResidentModels = 1` で非固定が辞書順先行すると
固定が復元されず、更新全体も失敗扱いで rollback した。

- 対策: 固定（`sessionPinned: true`）を先に復元し、失敗は即 raise（固定集合は必達）。
  非固定はベストエフォートで継続し、収まらない分は警告ログ＋skip（`replicaShortfalls`
  ではなく更新履歴の文脈。プールは固定を保持したまま残る）。
- `service.py` の legacy フォールバック（snapshot 空かつ単一 `loaded` のとき
  `sessionPinned: True` を捏造）は `False` へ。pool 無効時は facade が pin を無視する
  ため無作用、pool 有効時は到達不能なはずの経路で transient を固定化しない。

### 2.3 固定解除の残留（`management.py`）

`PUT /settings` で `keepLoaded` を外しても `slot.session_pinned` が残り、
行表示（`keepLoaded: keep_loaded or session_pinned`）が「固定」のまま、
TTL・LRU の回収対象にも戻らなかった。

- 対策: 新設 `_release_unpinned_sessions` が profiles の true→false/削除の遷移だけを
  検出し、該当モデルの `session_pinned` を落とす。profiles に属さない通常の手動ロード
  のセッション固定は保持する（文書「手動ロードは停止まで保持」の維持）。
  `PUT /settings`（`models` を含む場合）と `POST /settings/import` の両方から呼ぶ。
  preset 適用・setup 完了は profiles に触れないため対象外。

### 2.4 明示ロードの transient 選択肢（`management.py` / `cli.py`）

`POST /models/{id}/load` は `pin` を取らず常にセッション固定だった。

- 対策: body に任意 `pin: bool`（省略時は `true`＝従来動作、不正値は 422）を追加し
  `workers.load(..., pin=pin)` へ透過する。既存 GUI/CLI の送信形は変えないため
  挙動は不変。`mlxbarctl model load` に `--no-pin`（一時ロード、TTL 管理）を追加。
  将来 GUI が transient ロードを選ぶ準備であり、本版の表示・文書は変えない。

### 2.5 mlx-vlm tool 呼び出しの新 registry（`Workers/mlx_vlm_worker/adapter.py`）

`finalize()` は `mlx_vlm.tool_parsers` だけを試し、0.7.x 系（`mlx_vlm.tools` 配備、
`tool_parsers.py` 自体は 0.7.1 時点で既に不存在）では常に markup フォールバックへ
落ちていた。クラッシュはしないが、ネイティブ parser が使える環境で使われない。

- 対策: `mlx_vlm.tools` を先に試し、なければ旧 `tool_parsers`、最後に markup の
  三段フォールバック。旧 slot（registry 不足）では従来どおり markup になる。
  要求本体の例外契約は不変（失敗は握りつぶして fallback）。

### 2.6 導入時検査の deep 化（`runtimes/updater.py`）

stage 検査は top-level の `load` / `stream_generate` / `mx.set_memory_limit` だけを
確認し、要求時に初めて使う `apply_chat_template` 等の欠落は activate 後の生成失敗と
して表面化した。

- 対策: probe に hard 1件（vlm の `mlx_vlm.prompt_utils.apply_chat_template`。
  text 生成の全件が経由するため欠落は stage 失敗＝fail-closed）と soft 群（probe.json
  の `warnings[]` に記録し stage を止めない: vlm の `models.cache.make_prompt_cache` /
  `generate.PromptCacheState` / `apc.APCManager` / `tools._infer_tool_parser_from_processor`、
  lm の `models.cache.make_prompt_cache` / `sample_utils.make_sampler`）を追加。
  `contractVersion` は 2 のまま（worker protocol 不変、probe 形状の増分のみ）。
  旧 slot（0.6.15/0.7.1 系）が持つ module 群のため後方互換である。

## 3. 互換性・安全性の不変条件

- 設定スキーマ追加なし（`DEFAULTS` 不変、`schemaVersion` 1）。`pin` 省略時は v2.7.0 と
  同一動作。既存 `config.json` はそのまま読める。
- メモリ安全性：admission（`_admit`）・lane ガード（`_admit_concurrent`）・
  退避抑止（`leases > 0`）・critical pressure の固定解除は無変更。リロードの固定優先は
  admission の保護則（固定は追い出さない）と同方向であり、ゲートを弱めない。
- クラッシュ安全性：追加の `await` なし（`_release_unpinned_sessions` は同期のみ）。
  transient skip は警告ログのみで要求の成否に波及しない。probe 失敗は stage を
  中止し、既存 active slot に触れない（従来則）。
- UI：Swift 無変更。`keepLoaded` 表示則（`keep_loaded or session_pinned`→「固定」）は
  不変。本版はセッション固定の付与側を正したため、行の「固定」は固定プロファイルか
  明示の手動ロードに限定される。

## 4. 検証（`TEST_PLAN_v2.7.1.md` に記録）

- 新規 `Tests/test_v2_7_1_pin_runtime.py` 13件：リロードの固定優先＋transient skip＋
  固定失敗の raise、プリロードの二系統 pin、unpin のセッション解放と TTL 回収、
  CLI の `pin` 透過（既定 true／`--no-pin` false）、probe 契約（hard＋warnings＋
  構文妥当）、finalize の新 registry 優先と markup フォールバック。
- 既存回帰は 572＋13 の全緑（既知 fluke
  `test_ttl_starts_when_a_slow_load_finishes` を除く。クリーンツリーでも再現する
  asyncio タイミング依存で本版の回帰ではない）。
- 実機スモーク（最新 slot 直結、M1 Max）：lm-0.32.0 bf16／8bit、vlm-0.7.8 bf16／8bit の
  テキスト生成が完走し、vlm は2ターン目の memory reuse（`cached_tokens: 15`）を確認。
  量産モデルの応答品質（256M/135M の指示追従）は能力の問題で本版の対象外。
- `swift build` 成功（Swift 無変更のため再確認のみ）。
