# MLXBar v1.2.2

MLXBar v1.2.2 fixes a regression introduced in v1.2.1 where the menu bar popup effectively stopped rendering. Upgrading is strongly recommended for everyone on v1.2.1.

MLXBar v1.2.2は、v1.2.1で発生していた「メニューバーのポップアップが実質表示されない」回帰不具合を修正したリリースです。v1.2.1をお使いの方は更新を強く推奨します。

## Fixes

- **The menu bar popup collapsed to an almost-invisible size (roughly 420×10px) and effectively showed nothing.** This was caused by the `ScrollView` wrapper added in v1.2.1. `MenuBarExtra`'s `.window` style sizes the popup from its content's self-reported ideal size, but a `ScrollView` tends to expand to fill whatever height it's offered instead, which broke that size computation. The view has been reverted to a plain, unwrapped layout — the same approach that worked reliably in v1.0.0 and v1.1.0 — and verified on-device across multiple runs to render at the correct size consistently.

## Note

- We also investigated the Settings window (particularly the API Server page) being wider than its content needs. While testing a fix, we found that combining SwiftUI's `NavigationSplitView` with `.fixedSize(horizontal:vertical:)` on unrelated detail-pane text caused the sidebar list to render blank — a more serious, reproducible SwiftUI bug. Given the risk, that change was reverted; the Settings window remains as it was in the stable v1.2.1.

## 修正

- **メニューバーのポップアップが幅420×高さ10px程度のほぼ不可視のサイズに潰れ、実質何も表示されなくなっていました。** 原因はv1.2.1で追加した`ScrollView`ラッパーです。`MenuBarExtra`の`.window`スタイルは中身のビューが自己申告する理想サイズでポップアップの大きさを決めますが、`ScrollView`は渡された高さいっぱいに広がろうとする性質があり、サイズ計算が破綻していました。ScrollViewを使わない、v1.0.0・v1.1.0で実績のある構成に戻し、実機で複数回検証してポップアップが正しいサイズで安定して表示されることを確認しました

## 備考

- 設定画面（特にAPIサーバーページ）の余白が広すぎる点も調査しましたが、修正を試みる過程でSwiftUIの`NavigationSplitView`と、無関係な説明文への`.fixedSize(horizontal:vertical:)`の組み合わせにより、サイドバーの項目一覧が空白表示になる、より深刻で再現性のある不具合を発見しました。リスクを考慮しこの変更は見送り、設定画面は安定していたv1.2.1と同じ表示のままとしています

## Verification / 検証結果

- 124 unit and contract tests passed (no backend changes in this release; re-verified for safety).
- Swift debug and release builds passed.
- On-device verification: launched the built app, measured the menu bar popup's window geometry directly via macOS accessibility APIs across multiple open/close cycles (consistently ~420×270, matching its content), and visually confirmed both the popup and the Settings window (including its sidebar) render correctly.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, and DMG structure verified.

- 単体・契約テスト124件に成功しました（本リリースにバックエンドの変更はありませんが、安全のため再検証しました）。
- Swiftのdebug・releaseビルドに成功しました。
- 実機検証: ビルドしたアプリを起動し、macOSのアクセシビリティAPIでメニューバーポップアップのウィンドウサイズを複数回の開閉にわたって直接計測（一貫して約420×270、内容と一致）。ポップアップと設定画面（サイドバー含む）が正しく表示されることを目視でも確認しました。
- アプリ署名、同梱リソース、LaunchAgent、Coordinator／CLI、DMG構造を検証しました。

SHA-256 (`MLXBar-1.2.2.dmg`):

`f212560393d890aef6cdcb205e42e0bf37f929f278cab033b25fc661d7f27766`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.

このビルドはad-hoc署名です。初回起動時に「システム設定 > プライバシーとセキュリティ」から実行許可が必要になる場合があります。
