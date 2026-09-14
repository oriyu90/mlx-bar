# MLXBar v2.3.0 設計：実験的 Paged KV Cache

## 1. 目的と範囲

v2.3.0は、oMLXのブロック単位KV管理を参考に、mlx-barのmlx-lm Workerへ「検証済みの
連続prefixだけをSSDから復元する」実験的Paged KVキャッシュを追加する。機能、検討した代替案、
oMLX側の実ソース分析の詳細は `DESIGN_OMLX_STYLE_PAGED_KV_CACHE_PROPOSAL.md` を正本とする。

## 2. リリース不変条件

- `experimental.pagedKVCache.enabled` は既定 `false`。OFF時は専用環境変数、import、保存先作成を行わない。
- exact classがplain `mlx_lm.models.cache.KVCache` の全層だけを許可する。それ以外は従来経路へfail-closed。
- 256-token immutable blockとし、最低1 tokenのtailを再計算。hash chainとnamespaceの検査に成功した
  先頭の連続block以外は復元しない。
- cache layoutは実MLX tensorでstate setterをself-probeし、バージョン番号だけで判定しない。
- 保存と復元は、必要メモリとactive memoryが共に信頼できる場合だけ許可する。
- safetensorsとSHA-256 sidecarを0600、ディレクトリを0700とし、temporary file、`fsync`、atomic rename、
  起動時と次回store時の自己修復でクラッシュ整合性を保つ。
- 最初のmodel event前のみEXACT再試行を1回許可し、出力開始後は重複出力を避けるため再実行しない。
- `memoryTier` は `off` のみ受理。実測前のRAM tierとAdaptive MemoryのHybrid KV連携は本版で有効化しない。

## 3. 設定と運用

GUIとCLIでmaster、SSD、上限1–100 GB、branch reuseを設定する。master OFFで従属controlを
無効表示し、対応状態と理由、block/hit/restored token/memory skipを表示する。Paged専用削除は
確認ダイアログ後に、通常snapshotキャッシュと分離したRPCで実行する。

## 4. 検証

自動テスト、実MLX runtime、実モデルのfail-closed、隔離Coordinator、Swift Debug/Release、DMG構造と署名を
`TEST_PLAN_v2.3.0.md` に記録する。
