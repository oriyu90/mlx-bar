# MLXBar v2.8.1

v2.8.0 のパッチリリース。生成レーンの安定性と、GUI / CLI / 設定の
整合の精査指摘を解消します。

- 日付: 2026-10-18
- build: 54
- 署名: ad-hoc（Apple Developer Program 未加入のため公証なし。初回起動時に
  「システム設定 → プライバシーとセキュリティ」からの許可が必要な場合があります）

**既定値・API 形状・設定スキーマの変更はありません。** v2.8.0 からの
設定の引き継ぎ・ロールバックはそのまま動作します。

---

## 修正の要点

### 生成レーンが取り残される競合を修正

同時生成の反復テストで、`_gen_active_lanes` が 1 で止まり semaphore が
1 本不足したまま復帰しない状態が散発していました。原因は、親要求に
放棄されたレーン取得タスクが、orphan 回復によって解放されたレーンに
対して後から commit してしまう競合です（asyncio の cancel は、同じ瞬間に
回復処理が待ちを解放すると止まらないことがあります）。

- 取得タスクは全 await の再開時に放棄マーカを再確認し、commit せずに
  保持分だけを解放して戻ります。
- orphan 回復は同じ消失状態の **2 回連続観測**（0.5 秒幅）を要求するように
  なり、レーン取得直後の生きた要求を status poll が誤って回復しなくなりました。
- レーンを別要求に奪われた生成は黙って非直列化で続行せず
  `ENGINE_BUSY` (503, 再試行可能) で失敗します。

### coordinator のイベントループ詰まりを撤去

`status` の 1 秒ポーリングが `getaddrinfo`（LAN アドレス解決）を直接叩き、
hardware / preset / export / setup の各経路が `collect_hardware`
（`subprocess.run` × 3 + ディスク使用量、最大 15 秒）をイベントループ内で
実行していました。メモリ逼迫ホストでは全 API リスナーが秒単位で固まります。
両方ともワーカースレッドへ移し、応答形状は不変のまま詰まりを解消します。

あわせて `model unload` / `runtime activate` / `runtime rollback` が
in-flight 生成に阻まれたときに 500 ではなく 409 を返すようにしました
（チェックと実行の間に生成が始まる競合はサポートされた条件です）。

### mlx-vlm worker も tokenizers スレッドを抑制

v2.8.0 の `RAYON_NUM_THREADS` / `TOKENIZERS_PARALLELISM` の既定抑制は
mlx-lm adapter だけにあり、mlx-vlm worker は最初の tokenise でコア数分の
thread を起動していました。共通サーバーで抑制し、両 worker が同じ上限
（min(4, コア数)）を継承します。明示的な環境変数設定があればそちらが優先
されます（`setdefault`）。

### GUI / CLI パリティ

- `mlxbarctl config set-kb --chunk-overlap` が GUI と同じ交差検証
  （`0 <= overlap <= chunkSize/2`）を行い、失敗を先出しします。
- `mlxbarctl config set-lan false` が `requireToken` を黙って true に
  書き戻さなくなります（強制は LAN を有効にするときだけ）。
- `mlxbarctl config apply-preset custom` が GUI と同じ契約で使えます。
- ジョブ待ちに 30 分上限（`JOB_WAIT_TIMEOUT`）。GUI と同一です。
- coordinator が JSON 以外のエラー本文を返したとき、生テキストをそのまま
  見せます。

### 設定の一貫性

- **設定 import で LAN 構成が転送できない退行を修正（v2.6.0〜）。**
  `api.host` と `security.allowLan` は「必ずペアで変える」バリデーションの
  ため、leaf 単位の適用では片方ずつ失敗していました。両方を含む export は
  ひとつの候補として検証・適用します。
- `runtimes.*.autoCheck` / `checkIntervalHours` を消費する auto-check ループを
  coordinator に追加。v2.7.1 で導入されたこのキーは、これまで検証されるだけで
  何も参照していませんでした。定期チェックは手動チェックと同じ
  `runtime_check:{engine}` metadata を書き、**install は行いません**
  （明示操作のみ）。既定は不変（autoCheck off / 168h）。
- 設定検証の補強: `api.enabled` は bool、generation の数値は bool を拒否。
  いずれも既存値は影響しません。

### GUI 英語表示

v2.8.0 の待機延長欄の補足文言（accessibility hint）のキーに句点の不一致が
あり、英語 UI で裸キーが表示される可能性がありました。修正し、あわせて
`INVALID_IDLE_EXTENSION` / `MODEL_NOT_IDLE_MANAGED` / `JOB_WAIT_TIMEOUT` の
日英文言を追加しました。日本語 UI の表示は変わりません。

---

## 検証

- Python 自動回帰: **654 passed**（新規レーン整合性テスト 3 件を含む）。
- `swift build` / `plutil -lint` / `hdiutil verify` 通過。
- 詳細: [`DESIGN_v2.8.1.md`](DESIGN_v2.8.1.md) / [`TEST_PLAN_v2.8.1.md`](TEST_PLAN_v2.8.1.md)
