# MLXBar v2.3.0

v2.3.0は、oMLXのKV管理思想をmlx-barの安全性契約に合わせて再設計した、実験的Paged KV
キャッシュを追加します。plain mlx-lm `KVCache`のみを256-token blockでSSDへ永続化し、会話の
分岐やWorker再起動後に、検証できた最長の連続prefixを再利用します。

## 重要な安全性契約

- **既定で無効**です。有効化しない限り、専用コードのimportも保存先の作成も行いません。
- plain `KVCache` 以外は自動でfail-closedとなり、従来のwhole-prefix snapshotまたは通常EXACT生成を
  使います。
- active memoryまたは必要headroomが信頼できない場合、保存も復元も行いません。
- checksum、shape、dtype、offset、metadata、file size、tensor keyを検査し、破損データは隔離します。
  atomic renameの中断と孤立sidecarも自己修復します。
- 復元後の最初のモデル評価で失敗した場合、まだmodel eventを出していなければEXACTを1回だけ
  再試行します。出力開始後は重複応答を避けるため再実行しません。

## 操作

「Settings… > Cache > Experimental Paged KV Cache」または `mlxbarctl config set-paged-kv-cache`で
有効化できます。最初は小さなディスク上限で、対応状態とmemory safety skipを確認しながら使用して
ください。専用キャッシュはGUIまたは `mlxbarctl prompt-cache clear-paged` で削除できます。

詳細は `DESIGN_v2.3.0.md`、`DESIGN_OMLX_STYLE_PAGED_KV_CACHE_PROPOSAL.md`、
`TEST_PLAN_v2.3.0.md` を参照してください。

## 検証

- Python回帰テスト: 492 passed（固定順・ランダム順）
- Paged KV専用: 20 passed
- Swift Debug / Release build: 成功
- 組み込みruntime: mlx-lm 0.31.3 / MLX 0.32.2で実MLX tensorの保存・復元・分岐・破損回復・
  メモリ入場制御を確認
- 実モデル Ornith-1.5-9B-MLX-8bit: 非plain layoutとして正しくfail-closed

DMG SHA-256: `3fdca5270d6bce9029a89e21bf392456ddb3d75395471a16e10c77e9affc10d7`

> 本ビルドはad-hoc署名でApple公証は行っていません。初回起動時は「システム設定 →
> プライバシーとセキュリティ」から起動許可が必要になる場合があります。
