# mlx-bar 保守メモ

> 公開物（README・紹介サイト等）には出さない、次回以降の開発向けメモ。
> [common-rules-document](https://github.com/oriyu90/common-rules-document/blob/main/common%20rules.md) ルール6に基づき作成。

## 未解決の課題

### 設定画面（Settings）の横幅が広すぎる
- APIサーバーページなど、コンテンツが約650pxしか使っていないのにウィンドウ幅が約900pxあり、余白が目立つ。この「無駄な余白」自体はv1.2.3でも未解消（モデルタブの横はみ出し・見切れの方を修正した）。
- 次に試すなら: `NavigationSplitView`をやめて`HSplitView`やカスタムレイアウトに置き換える方向で検討する。

### SwiftUIの既知の罠（踏まないよう記録）
- `NavigationSplitView`のdetailペイン内のテキストに`.fixedSize(horizontal: false, vertical: true)`を追加すると、**無関係のサイドバー（項目一覧）が空白表示になる**現象を複数回・複数パターンで再現した（[Sources/MLXBar/Settings/MLXBarSettingsView.swift](Sources/MLXBar/Settings/MLXBarSettingsView.swift)）。v1.2.3でモデルタブの説明文を折り返させる際に同じ現象を実際に踏んだ（サイドバーが空白化）。**`.fixedSize`を使わず、`.frame(maxWidth: .infinity, alignment: .leading)`だけで折り返しを実現する**ことで回避し、複数回の実機確認でサイドバーが安定して表示されることを確認した。折り返しが必要な説明文には今後もこの方式を使うこと。
- `LabeledContent`は同一Form内（少なくとも同一セクション内）の行でラベル用の配置カラムを共有する。幅無制限の`Slider`など「際限なく広がりたい」コントロールを`LabeledContent`と同じForm内に置くと、その共有カラム計算が壊れ、長いラベルが左端で欠けたり、キャプションの`Text`がウィンドウ幅を無視してそのまま突き抜けたりする（v1.2.3で発見・修正）。幅が可変・無制限になり得る行は`LabeledContent`を使わず、`VStack(alignment: .leading)`でラベルとコントロールを別行に分離し、`.frame(maxWidth:)`で明示的に幅を制限すること。
- `.navigationSplitViewColumnWidth(min:ideal:max:)`はidealではなくmaxに近い値で初期サイズが決まる、かつ状況によりサイドバーの初期表示が不安定になることを確認。
- `MenuBarExtra`の`.window`スタイル配下では`ScrollView`はコンテンツの理想サイズを正しく報告できず、ポップアップが数px程度に潰れることがある（v1.2.1で実際に発生、v1.2.2で素の`VStack`に戻して解消）。同様の構成を作る際は要注意。一方、`NavigationSplitView`のdetailペイン（`Settings`ウィンドウ等）ではこの制約はなく、`Form`が縦方向にスクロールしない場合は素直に`ScrollView`で包んで問題ない（v1.2.3で確認）。

### Coordinatorのシャットダウンとバックグラウンドジョブ
- ランタイム自動インストール（`AppState.install_missing_runtimes()`）が起動するジョブは、`uv`のサブプロセスを`start_new_session=True`で独立したプロセスグループとして起動する（`RuntimeUpdater._command`）。これは明示的なジョブキャンセル（`/cancel`エンドポイント）時に`killpg`で確実に止められるようにするための設計。
- ただし、v1.2.2以前はCoordinatorの終了処理（`main.py`の`serve()`の`finally`節）が実行中のジョブを一切キャンセルしていなかったため、SIGTERM/SIGKILLでCoordinatorが終了しても、インストール中の`uv`プロセスは孤児化してバックグラウンドで動き続けていた（グローバル共有キャッシュに書いていたv1.2.2以前は実害が目立たなかった）。
- v1.2.3で`JobManager.cancel_all()`を追加し、`serve()`の`finally`節で最初に呼ぶよう修正。複数ジョブは`asyncio.gather`で並行キャンセルすること（逐次だと5秒×ジョブ数の待ち時間になり、テストのタイムアウトと競合して不安定になることを結合テストで確認済み）。

## リリース時のバージョン文字列更新チェックリスト

新バージョンをリリースする際、以下のファイルの版数表記をすべて更新すること（漏れやすい）:

- `Coordinator/pyproject.toml`（`version`）
- `Packaging/Info.plist`（`CFBundleShortVersionString`・`CFBundleVersion`）
- `scripts/build-release.sh`（`VERSION=`）
- `scripts/verify-release.sh`（デフォルトの`VERSION`フォールバック値）
- `README.md`（先頭のVersion表記、DMGファイル名2箇所）
- `CHANGELOG.md`（新バージョンの節を先頭に追加）
- `RELEASE_NOTES_v{version}.md`（新規作成、SHA-256は実ビルド後に追記）
- `website/index.html`（`softwareVersion`のJSON-LD、kicker、ダウンロード手順のDMGファイル名、動作環境テーブルの最新版）

## ウェブサイトの多言語対応について

- `website/index.html`は日本語・English・中文・Português の4言語に対応（2026-08-19対応、common rulesルール3準拠）。
- 実装はクライアントサイドJS（`window.MLXBarI18n`）による切り替え方式。ブラウザ言語を自動判定し、`localStorage`に保存した選択を優先する。
- HTML本文中の`data-i18n="key"`が翻訳対象の目印。`<script>`内の`I18N`オブジェクト（`ja`/`en`/`zh`/`pt`の4キー）に翻訳文を保持している。
- **本文コピーを変更する際は、raw HTML（日本語）と`I18N.ja`/`I18N.en`/`I18N.zh`/`I18N.pt`の計5箇所すべてを揃えて更新すること。** 1箇所でも漏れると、その言語に切り替えたときだけ古い文言が残る。
- `<code>`や`<b>`などのHTMLタグを含む翻訳文には`data-i18n-html="true"`を付け忘れないこと（付け忘れるとタグが文字列としてそのまま表示される）。

## Cloudflare Pages

- プロジェクト名`mlx-bar`はGit連携（GitHub `oriyu90/mlx-bar`、`main`ブランチ）で作成済み。`destination_dir`は`website`、`build_command`は空。設定変更は不要（2026-08-19時点でAPI経由確認済み）。

## ランタイムインストールのuvキャッシュについて

- mlx-lm・mlx-vlmのインストール（`Coordinator/mlxbar/runtimes/updater.py`）は`UV_CACHE_DIR`を`Application Support/MLXBar/uv-cache`に固定している（v1.2.3〜）。「すべてのデータを削除して終了」がMLXBar専用フォルダを丸ごと削除する対象に含めるための変更で、他プロジェクトが使う共有の`~/.cache/uv`は一切触らない。
- v1.2.2以前でインストール済みのユーザーは、共有キャッシュ側に残った古いmlx-lm/mlx-vlmのダウンロード分がそのまま残る。これはMLXBar固有のデータではなく他プロジェクトとも共有される領域のため、アプリ側から追跡・削除する対象にはしない方針。

## その他

- （2026-08-19時点）開発用Macの重複コピー`MLXBar 2.app`は解消済み。今後また似た状態に気付いたら削除を検討。
