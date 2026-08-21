# MLXBar v1.4.1 Test Plan

v1.4.1は、OpenAI互換SSEクライアントが生成中またはキュー移行中に切断しても、後続要求が永久待機しないことを保証する回復性リリースです。

## 受入基準

1. OpenAI Chat Completions、クイックチャット、APIアクセス記録wrapperのSSE切断時に、内側の生成処理まで終了が伝播する。
2. 生成枠の所有者を要求単位で追跡し、所有者本人以外の終了処理がロックを解放できない。
3. 所有者がactiveにもqueuedにもいない孤立ロックだけを自動回復する。
4. 正常なqueue→active引き継ぎを孤立と誤認せず、同時生成数1を維持する。
5. 既に待機している要求が孤立ロックの回復後に起床して完了する。
6. 多数の要求と切断が重なっても、最終状態がactive 0、queued 0、lock idleになる。
7. OpenAIのstream/non-stream、thinking、tools、Prompt Cacheの既存動作を退行させない。
8. 診断情報に生成ロックの状態と回復回数を出し、本文や要求IDなどの機密情報は出さない。

## 障害注入シナリオ

- activeもqueuedもない状態でlockedの生成枠を作り、状態取得と次要求の双方から回復する。
- queued ownerがロック取得直後、active登録直前のhandoff状態を作り、回復されないことを確認する。
- 古い要求の終了処理を遅延させ、新しいownerのロックを解放できないことを確認する。
- 孤立ロックの後ろに要求を待機させ、queue heartbeatによる回復と要求完了を確認する。
- OpenAI SSE、管理SSE、APIアクセス記録wrapperを途中で閉じ、重なった内側generatorの`finally`まで完了することを確認する。
- 100要求を並行投入し、約3分の1を取得前後でキャンセルする。

## 完了条件

- Pythonの単体・契約・結合・障害注入・ネットワークテストが警告なしで完走する。
- Swift Debug／Release build、plistとローカライズの検証が成功する。
- DMG内のアプリ版が1.4.1、build 18で、arm64、署名、CLI、同梱Coordinator、ソース収録、ディスクイメージ整合性が検証される。
- 同梱Coordinatorを隔離HOMEで起動し、管理APIとCLIが応答する。既存v1.4.0が公開ポートを使用中の場合、公開APIのport conflictは期待結果として区別する。
- 公開assetのSHA-256がローカル成果物と一致する。

## 2026-08-21 実施結果

- 実機の停止状態（active 0、queued 1、Worker idle）をモデル不要の障害注入で再現し、孤立ロック回復後に後続要求が完了した。
- 正常handoff、owner境界、待機済み要求、重なったSSE wrapperの明示終了、100並列切断stressを含むPythonテスト171件が成功した。
- Xcode 26.6でSwift Debug／Release buildが成功し、plistとローカライズ資材の検証も成功した。
- DMGを読み取り専用でマウントし、アプリ版1.4.1、build 18、arm64、ad-hoc署名、CLI、同梱Coordinator、v1.4.1テスト計画と修正済みsource、`__pycache__`非収録、ディスクイメージ整合性を確認した。
- 同梱Coordinatorを公開API・自動runtime install無効の隔離HOMEで起動し、同梱CLIから`service=running`、active 0、queued 0、`generationLockState=idle`、回復0を確認して正常終了した。
- ローカルDMGのSHA-256をrelease notesへ記録し、公開assetとの一致はrelease upload後に確認する。
