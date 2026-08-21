# MLXBar v1.4.0 Test Plan

v1.4.0は永続Prompt Cacheを正式提供するリリースです。以下を公開判定の受入基準とします。

## 受入基準

1. Qwen3.8-27Bで、cold要求後にcacheファイルが専用namespaceへ作成される。
2. Workerを再作成した次の同一ZCode要求が`cache_tier=disk`となり、coldよりfirst-token時間が短い。
3. 最初のユーザー文を変更しても、system/toolsが同一なら大部分のprefixがdisk hitする。
4. 同一Workerの次ターンは`cache_tier=memory`となり、v1.3.7の速度を退行させない。
5. messages、tools、tool calls、thinking、stream/non-streamのOpenAI互換結果がcache tierで変化しない。
6. 画像要求へtext cacheを渡さない。
7. 壊れたcache、容量上限、低メモリ、生成キャンセル、Worker強制終了から安全に回復する。
8. モデル重み、tokenizer/chat template、mlx-vlm版の変更後に旧namespaceを使用しない。
9. 設定画面のRAM/disk消去と全データ削除が対象cacheを削除する。

## 計測項目

- model load時間
- first-token時間
- prompt tokens / cached tokens
- prompt TPS / generation TPS
- cache tier（cold / memory / disk）
- disk snapshot作成時間・復元時間・使用量
- generation前後のactive/cache/peak memory
- WorkerログのAPC reject、fallback、disk eviction

## シナリオ

- cold → memory hit → Worker再作成 → disk hit
- 同一system/toolsで短い質問を変更
- tool schemaを1項目変更し、誤hitしないことを確認
- thinking有効/無効、stream有効/無効、tool call有無
- cacheファイルを破損させ、応答前fallbackを確認
- 1 GB上限で複数prefixを作成し、LRU evictionを確認
- disk cache無効、RAM消去、disk消去、全データ削除
- 生成中キャンセル、Worker終了、再起動
- 画像要求とテキスト要求の交互実行

## 完了条件

- Python全テスト、Swift release build、署名、DMG検証が成功する。
- Qwen3.8-27Bのdisk hitで明確なfirst-token改善を実測する。
- cold/メモリ安全性とOpenAI/ZCode互換性に重大な退行がない。
- 未解決の問題と再現手順を`mlx-bar.md`へ記録する。
- FastAPI／Starletteテストを非推奨警告なしで完走し、警告が再発した場合はテストを失敗させる。

## 2026-08-21 中心シナリオ実測

実機`Qwen3.8-27B-MLX-8bit`、mlx-vlm 0.6.15、23 toolsを含む15,852-token入力で測定しました。モデルloadは12.912秒でした。

| 経路 | elapsed | prompt tokens | cached tokens | prompt TPS |
| --- | ---: | ---: | ---: | ---: |
| cold | 56.106s | 15,852 | 0 | 290.96 |
| Disk APC（同一質問） | 1.531s | 15,852 | 15,596 | 12,673.68 |
| Disk APC（最初の質問を変更） | 1.414s | 15,861 | 15,596 | 13,761.29 |
| PromptCacheState（継続会話） | 0.262s | 15,879 | 15,862 | 68,704.27 |

Disk managerをcloseし、PromptCacheStateを再作成してDiskBlockStoreのindexを読み直すことでWorker再起動相当を再現しました。`num_blocks=0`、resident bytes 0、APC reject/fallback 0を確認しました。cold後のexact snapshotは約2.37 GB（2境界）であり、既定5 GBでは巨大prefixを複数保持するとLRU evictionが起こり得ます。

中心目的（再起動後の最初の会話、最初の質問変更、現行RAM速度の維持）は合格です。破損ファイル・設定値・API/DB移行は自動テスト済みです。長時間連続運転、1 GBでの複数巨大prefix eviction、実際のZCodeワークスペースを跨ぐA/B、強制終了中の大容量writeはDMG利用時の追加ストレス項目です。
