# MLXBar v1.5.0 Test Plan

v1.5.0は、27BクラスのモデルをZCodeから常用する前提での設計精査を反映した強化リリースです。メモリ安全性、クラッシュ安全性、プロンプトキャッシュ効率、OpenAI互換性の4面を対象とします。

## 受入基準

1. クライアント切断時にWorkerのadapter生成器が必ず閉じられ、その終了処理がMLXスレッド上で実行される。
2. 待機中にモデルが降ろされた要求がHTTP 409 `MODEL_NOT_LOADED`で終わり、孤立ロック回復に頼らず生成枠を解放する。
3. 実行中または待機中の生成があるあいだ、モデルのロード・アンロードが`ENGINE_BUSY`で拒否され、明示的な強制実行だけが迂回できる。
4. mlx-lmモデルでRAM層とディスク層のプロンプト再利用が働き、Worker再起動をまたいでも共通prefixを再利用する。
5. 保存されるディスクprefixがプロンプト長を超えず、モデル自身の応答を含まない。
6. 生成中にメモリ上限へ達した場合、Workerプロセスではなく該当要求だけが`MEMORY_PRESSURE`で停止する。
7. メモリ判定が高水位ではなく現在の常駐サイズを使い、一度の大きな生成が以後の要求を恒久的に拒否しない。
8. 生成の安全上限を超えてもモデルがロードされたまま維持され、Workerの完全な無応答時だけプロセスが再起動される。
9. `max_tokens`で切り詰められた応答が`finish_reason: "length"`で返る。
10. `stop`シーケンスが分割配信をまたいで検出され、停止位置より後ろのテキストからtool callが生成されない。
11. ランタイムのtool parserが解釈する記法が本文へ漏れず、tool callとの二重配信が起きない。
12. `response_format`の非対応値と`logprobs`がHTTP 400で拒否され、`response_format: {"type": "text"}`は従来どおり動作する。
13. 古いプロンプトキャッシュ世代が`keepGenerations`に従って回収される。
14. ランタイムの自動更新チェックが既定で無効である。
15. 既存のOpenAI stream／non-stream、thinking、tools、mlx-vlmのプロンプトキャッシュ動作を退行させない。

## 障害注入・実機シナリオ

- 実uvicorn + Unixドメインソケットで生成中にクライアントを切断し、生成器の終了とトークン生成の停止を確認する。未修正ビルドで同じ手順が失敗することも確認する。
- 生成中の要求を保持したまま`unload()`を呼び、待機列の要求が409で終わり生成枠が即座に解放されることを確認する。
- 実モデルへZCode形式（多数のtools定義＋長いsystem prompt）を送り、cold／RAM hit／末尾変更／Worker再起動後のディスクhitを実測する。
- 古いキャッシュ世代を複数用意し、現行世代と直近1件だけが残ることを確認する。
- メモリ統計を逼迫状態に固定し、生成中の停止と`MEMORY_PRESSURE`の返却を確認する。
- `ru_maxrss`が高水位のまま下がらないことを実測し、判定が現在値を使うことを確認する。
- 停止シーケンスの直後にtool記法を出力させ、本文にもtool callにも現れないことを確認する。

## 完了条件

- Pythonの単体・契約・結合・障害注入・ネットワーク・並行stressテストが非推奨警告なしで完走する。
- Swift Debug／Release buildが成功する。
- DMG内のアプリ版が1.5.0で、arm64、署名、CLI、同梱Coordinator、ソース収録、ディスクイメージ整合性が検証される。
- 同梱Coordinatorを隔離HOMEで起動し、管理APIとCLIが応答する。
- 公開assetのSHA-256がローカル成果物と一致する。

## 2026-08-22 実施結果

- Pythonテスト193件（v1.4.1の171件から+22件）が非推奨警告なしで成功した。
- 実uvicorn + UDSで4行受信後に切断し、Workerが1トークン以内で停止し、生成器が`slow-test-mlx_0`（MLXスレッド）で閉じられることを確認した。未修正のv1.4.1では生成器が一切閉じられないことも確認した。
- 待機中unloadの再現で、要求は`MODEL_NOT_LOADED` 409で終わり、`generation_lock.locked()=False`、`generation_owner=None`、`generationLockState=idle`、回復回数0だった。修正前は`AttributeError`かつ`inconsistent`だった。
- 実機`llm-jp-4-32b-a3b-thinking-4bit`（mlx-lm、16.8 GB）へ23 tools・8,024 tokenの合成ZCode入力を送信：

  | 条件 | tier | first token | 再利用token |
  |---|---|---|---|
  | 初回 | cold | 5.563 s | 0 |
  | 同一プロンプト再送 | memory | 0.051 s | 8,023 |
  | 最初のユーザー文を変更 | memory | 0.116 s | 8,013 |
  | Worker再起動後 | disk | 0.673 s | 7,768 |
  | 続けて末尾を変更 | memory | 0.116 s | 8,013 |

- 古いキャッシュ世代3件に対し`keepGenerations=2`で、現行世代と直近1件だけが残った。
- `mmap`で400 MBを確保・解放し、`ru_maxrss`が高水位のまま、`process_rss_bytes`が実際に低下することを確認した。
- Xcode 26.6でSwift Debug／Release buildが成功した。
- DMGを読み取り専用でマウントし、アプリ版1.5.0（build 19）、arm64、ad-hoc署名、`__pycache__`非収録、v1.5.0テスト計画と新規`prompt_cache.py`の収録を確認した。ビルドスクリプトがテスト計画のファイル名をv1.4.1で固定していたため、`$VERSION`連動へ修正した。また`CFBundleVersion`の更新漏れ（18のまま）を保守メモのチェックリスト照合で検出し、19へ修正して再ビルドした。
- 同梱Coordinatorを公開API・自動runtime install無効の隔離HOMEで起動し、同梱CLIから`service=running`、active 0、queued 0、`generationLockState=idle`、回復0、`settingsRecoveredFrom=null`を確認して正常終了した。
- ローカルDMGのSHA-256を`8bf212103ba2be3d4fbd83066cb250a4e41c60041105fd374aff0390348b1831`としてrelease notesへ記録し、公開assetとの一致はrelease upload後に確認する。

## 未実施・既知の範囲

- 27B実機（`Qwen3.8-27B-MLX-8bit`、mlx-vlm経路）でのメモリ上限到達、長時間連続運転、実際のZCodeワークスペースを跨ぐA/Bは未実施。mlx-vlm側のプロンプトキャッシュ動作はv1.4.0から変更していない。
- mlx-vlmの中断時プロンプトキャッシュは、部分ロールバックではなく従来どおり全体を破棄する。安全側の挙動であり、ディスク層が残るため次要求はcoldではなくdisk hitになる。
