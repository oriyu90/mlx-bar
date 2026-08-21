# MLXBar v1.4.1

MLXBar v1.4.1 fixes a generation-queue deadlock that could leave every later ZCode request waiting forever after an OpenAI-compatible SSE connection disconnected. It preserves request serialization and automatically recovers only a truly orphaned generation slot.

MLXBar v1.4.1は、OpenAI互換SSE接続が途中で切れた後、以降のZCode要求が永久に待機することがある生成キューのデッドロックを修正します。生成の直列性を維持し、本当に孤立した生成枠だけを自動回復します。

## Highlights / 主な変更

- Explicitly propagates closure through the API logging, OpenAI, and Quick Chat stream wrappers when an SSE client disconnects.
- Assigns every generation slot an owner request ID; stale cleanup cannot release another request's slot.
- Recovers a locked slot only when its owner is neither active nor queued and no generation is active.
- Preserves the owner registration across the queue-to-active handoff, preventing false recovery and parallel model execution.
- Adds privacy-safe `generationLockState` and `generationLockRecoveries` diagnostics.
- Adds cancellation, handoff, stale-owner, queued-wakeup, SSE-close, and 100-request concurrency stress tests.

- SSE切断時に、APIアクセス記録、OpenAI API、クイックチャットの重なったwrapperを通して内側の生成ストリームまで明示的に終了を伝えます。
- 生成枠へ要求ID付きの所有権を割り当て、古い終了処理が別要求の生成枠を解放できないようにしました。
- ownerが実行中にも待機中にも存在せず、実行中要求もない場合だけ孤立ロックを回復します。
- queueからactiveへの移行中もowner登録を維持し、誤回復とモデルの並列実行を防ぎます。
- 本文を含まない`generationLockState`と`generationLockRecoveries`診断を追加しました。
- 切断、handoff、古いowner、待機要求の起床、SSE終了、100要求の並行stressを回帰テストへ追加しました。

## Verification / 検証

- 171 Python unit, contract, integration, fault-injection, network, and concurrency tests passed with zero warnings.
- Swift Debug and Release builds passed using Xcode 26.6.
- App signature, embedded versions/resources, CLI, source package, DMG structure, and disk image integrity were verified.

- Pythonの単体・契約・結合・障害注入・ネットワーク・並行stressテスト171件が警告なしで成功しました。
- Xcode 26.6でSwift Debug／Release buildが成功しました。
- アプリ署名、内蔵バージョン／資材、CLI、ソース収録、DMG構造、ディスクイメージ整合性を検証しました。

SHA-256 (`MLXBar-1.4.1.dmg`):

`d03675f3117f5bf0906bc9197632a4e821cd4dc93da7a5ccf5162222f60adcad`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
