# Release Notes v2.7.0

## 生成後の待機表示・2つ目の並列生成・非固定モデルの待機時間

### 直したこと

- **生成が終わったのに「応答生成中」のままになることがある不具合を修正**しました。クライアントの
  切断などで取り残された生成状態を、状態取得のたびに自動回復します。メニューバーは次の更新周期で
  待機中に戻ります。
- **同一モデルの2つ目の同時生成が使えないことがある不具合を修正**しました。空きレプリカがなく
  メモリに余裕があれば、レプリカを1つ自動追加して並行します。余裕がなければ従来どおり順番待ちに
  なります。`最大常駐モデル数`は異なるモデルの種類数の上限のままです。
- **同一モデルの複数レプリカの表示を集約**しました。2つ目以降の生成中・順番待ちが一覧に隠れなく
  なりました。

### 非固定モデルの待機時間

- 待機時間の設定自体（「Settings…」→モデル→「非固定モデルの待機時間」、30〜86,400秒）は従来
  どおりで、再起動なしで反映されます。v2.7.0 では待機中の行に解放までの残り時間を表示し、
  `mlxbarctl model resident` でも待機時間と解放予定を確認できるようにしました。

### 互換性

- 設定の追加のみで、既定値の変更はありません。既存の `config.json` はそのまま使えます。
- `replicas = 1`・`generationConcurrency = 1` の構成の挙動は従来どおりです。

---

## Release Notes v2.7.0 (English)

### Fixed

- **Rows stuck on "Generating" after a response finished.** Orphaned generation state (e.g. from a
  disconnected client) is now self-healed on every status poll, so the menu bar returns to Idle on
  the next refresh.
- **A 2nd concurrent generation on the same model sometimes never ran.** When no replica is free
  and memory allows, one replica is added automatically so both run in parallel. Without headroom it
  waits its turn as before. Maximum resident models still caps distinct model kinds.
- **Replica states are now aggregated per model row**, so activity on 2nd and later replicas is no
  longer hidden.

### Idle time for unpinned models

- The setting itself (Settings → Models → "Idle time for unpinned models", 30–86,400 seconds,
  no restart needed) is unchanged. v2.7.0 adds the remaining time to idle rows and shows the idle
  time plus per-model expiry in `mlxbarctl model resident`.

### Compatibility

- Additive changes only; no default values changed. Existing `config.json` files keep working.
- Behaviour with `replicas = 1` or `generationConcurrency = 1` is unchanged.
