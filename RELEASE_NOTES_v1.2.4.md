# MLXBar v1.2.4

MLXBar v1.2.4 fixes a reported bug where the menu bar icon would flash and briefly disappear when clicked, plus four related stability, memory-safety, and crash-safety issues found while investigating it. Upgrading is recommended for everyone.

MLXBar v1.2.4は、「メニューバーのアイコンをクリックすると消える」という報告を受けて調査し、その原因を修正したリリースです。あわせて調査の過程で見つかった、関連する安定性・メモリ安全性・クラッシュ安全性の課題4件も修正しています。更新を推奨します。

## Fixes

- **The menu bar icon could flash and briefly disappear right after clicking it.** While the menu is open, `MenuBarView` calls `refreshStatus()` every second. That handler reassigned every `@Published` property unconditionally on each poll, even when nothing had changed, so SwiftUI saw a "change" every second and kept re-rendering the status item's `Label` and the popover body. `MenuBarExtra`'s `.window` style is already known to render unreliably under rapid content churn — that's what collapsed the popover to a few px in v1.2.1 (see [mlx-bar.md](mlx-bar.md)) — and this was the same instability showing up as the icon itself. Each field is now written only when the polled value actually differs.
- **The menu bar icon stayed identical (`cpu`) whether the service was running or stopped.** The icon's final branch was `serviceRunning ? "cpu" : "cpu"` — both sides identical, so the stopped state had no icon of its own. It now shows `moon.zzz` when the service is stopped.
- **A failed launch of the streaming curl process (used for chat generation) could leak the pipe handlers it had already registered.** `CoordinatorClient.stream()` threw before reaching its cleanup code whenever `process.run()` failed, leaving the `Pipe`'s handlers — and everything they captured — never released. Cleanup now runs in a `defer`.
- **"Remove all data and quit" could spin forever if `launchctl bootout` or `defaults delete` stopped responding.** Unlike the curl-based `request()` calls, these had no timeout at all. `runProcess` now supports a watchdog timeout, guarded by a `SingleResume` helper so a race between normal termination and the watchdog can't resume the same continuation twice.
- **The Runtime Manager's slot and history lists used array indices as their SwiftUI `ForEach` identity.** A reorder or count change in either list could make SwiftUI reuse a row's animation/state for what was actually a different entry. Both lists now key on the backend's own identifiers instead.
- Several `Task` closures captured `self` unconditionally; `attachRuntimeJob`, `copyLoadedModelName`, and the chat-generation streaming callback now capture it weakly. Low real-world impact, fixed defensively.

## 修正

- **メニューバーのアイコンをクリックした直後、フラッシュして一瞬見えなくなることがありました。** メニューを開いている間、`MenuBarView`は1秒間隔で`refreshStatus()`を呼びますが、このハンドラは値が変化していなくても`@Published`プロパティを毎回無条件に再代入していたため、状態が同じでも毎秒SwiftUIに「変更あり」が伝わり、ステータスバーの`Label`とポップアップ本体が再描画され続けていました。`MenuBarExtra`の`.window`スタイルは内容が頻繁に変化すると描画が不安定になることが既に分かっており（v1.2.1でポップアップが数px幅に潰れた原因、[mlx-bar.md](mlx-bar.md)参照）、今回のアイコン消失も同じ不安定さの現れでした。ポーリング結果が実際に変化した場合だけ書き込むよう修正しました。
- **サービス停止中も、メニューバーアイコンが起動中と同じ`cpu`のままでした。** アイコンを決める最後の分岐が`serviceRunning ? "cpu" : "cpu"`という左右同一の三項演算子になっており、停止状態専用のアイコンが存在していませんでした。停止中は`moon.zzz`を表示するようにしました。
- **チャット生成のストリーミングに使うcurlプロセスの起動に失敗すると、登録済みのPipeハンドラが解放されずリークすることがありました。** `CoordinatorClient.stream()`は`process.run()`が失敗した場合、ハンドラ解除コードに到達する前に例外を投げており、`Pipe`のハンドラとそれが捕捉していたものが解放されない経路がありました。`defer`で解除を保証しました。
- **`launchctl bootout`や`defaults delete`が応答しなくなった場合、「すべてのデータを削除して終了」がスピナー表示のまま止まらなくなる可能性がありました。** curlベースの`request()`呼び出しと異なり、これらの呼び出しにはタイムアウトが一切ありませんでした。`runProcess`にウォッチドッグ方式のタイムアウトを追加し、正常終了とタイムアウト発火が競合しても同じcontinuationを二重にresumeしないよう`SingleResume`ヘルパーで保護しています。
- **ランタイム管理画面のスロット・更新履歴一覧が、配列のインデックスをSwiftUIの`ForEach` IDとして使っていました。** 一覧の並び替えや件数変化があると、行のアニメーション・状態が別のエントリのものと入れ替わる可能性がありました。両方ともバックエンド自身が返すIDをキーにするよう修正しました。
- 複数の`Task`クロージャが`self`を無条件に強参照していました。`attachRuntimeJob`・`copyLoadedModelName`・チャット生成のストリーミングコールバックを弱参照に変更しています。実害は小さいですが、防御的に修正しました。

## Verification / 検証結果

- 125 unit, contract, and integration tests passed unchanged (Coordinator/Workers code was not touched this release). One integration test is a pre-existing flake under full-suite parallelism (passes standalone every time); not a regression.
- Swift debug build passed; on-device verification: relaunched the app repeatedly, confirmed the menu bar icon stays present and stable while opening/closing the menu, and confirmed the app still gains keyboard focus correctly as a packaged `.app`.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約・結合テスト125件が無変更で成功しました（今回はCoordinator／Workers側を変更していません）。結合テスト1件はフルスイート並列実行時のみ稀に発生する既知のflaky testで、単体実行では毎回成功しており今回のリグレッションではありません。
- Swiftのdebugビルドに成功。実機検証として、アプリを繰り返し起動し、メニューの開閉中もメニューバーアイコンが常に表示され続けること、パッケージ化された`.app`として正しくキーボードフォーカスを得ることを確認しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.2.4.dmg`):

`80099c3d01f08a9a7feb6f7eee67b4a5d7195f89464ca8ea14ba306d7efc1b8a`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
