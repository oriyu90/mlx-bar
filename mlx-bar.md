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
- 上記と同系統の問題として、`.window`スタイルの`MenuBarExtra`は**内容が高頻度で更新されるだけでもステータスバーの表示が不安定になる**（v1.2.4で発見・修正）。`MenuBarView`はメニューを開いている間`refreshStatus()`を1秒間隔で呼ぶが、`@Published`プロパティは値が変化していなくても代入するだけで`objectWillChange`を発火するため、同じ状態でも毎秒SwiftUIへの再描画要求が発生し、ステータスバーの`Label(shortStatus, systemImage: icon)`とポップアップ本体が再描画され続けてアイコンがちらつく・一瞬消えるという形で表面化した。`MenuBarExtra`をバックエンドとする`ObservableObject`を高頻度ポーリングで更新する場合は、`if 現在値 != 新しい値 { 現在値 = 新しい値 }`のように**値が実際に変化したときだけ代入する**こと（`MenuBarViewModel.setIfChanged`参照）。

### Coordinatorのシャットダウンとバックグラウンドジョブ
- ランタイム自動インストール（`AppState.install_missing_runtimes()`）が起動するジョブは、`uv`のサブプロセスを`start_new_session=True`で独立したプロセスグループとして起動する（`RuntimeUpdater._command`）。これは明示的なジョブキャンセル（`/cancel`エンドポイント）時に`killpg`で確実に止められるようにするための設計。
- ただし、v1.2.2以前はCoordinatorの終了処理（`main.py`の`serve()`の`finally`節）が実行中のジョブを一切キャンセルしていなかったため、SIGTERM/SIGKILLでCoordinatorが終了しても、インストール中の`uv`プロセスは孤児化してバックグラウンドで動き続けていた（グローバル共有キャッシュに書いていたv1.2.2以前は実害が目立たなかった）。
- v1.2.3で`JobManager.cancel_all()`を追加し、`serve()`の`finally`節で最初に呼ぶよう修正。複数ジョブは`asyncio.gather`で並行キャンセルすること（逐次だと5秒×ジョブ数の待ち時間になり、テストのタイムアウトと競合して不安定になることを結合テストで確認済み）。

### ad-hoc署名とlaunchdラベル競合（v1.3.0で対策・完全解決ではない）
- このアプリはDeveloper IDを設定しない限りad-hoc署名（`codesign --sign -`）でビルドされる（`scripts/build-release.sh`）。ad-hocの署名IDはビルドごとに変わる。
- `com.yukiorita.MLXBar.Coordinator`というlaunchdラベルを、`SMAppService.agent(...)`経由の登録と、手動フォールバックplist（`~/Library/LaunchAgents/`）の**2つの経路が共有**している。過去にインストールした別ビルド（署名IDが異なる）の登録が、Finderでの上書きインストールなど「すべてのデータを削除して終了」を経由しない入れ替えで残っていると、新しいインストールの起動時にOSがLaunch Constraint Violationとして`SIGKILL (Code Signature Invalid)`を出すことがある（v1.2.4リリース検証中に実機で発生・確認）。
- v1.3.0で`CoordinatorClient.startService()`に、初回の健全性チェックが失敗した場合の防御的な回復ステップ（両登録経路を`launchctl bootout`してから再登録・再試行）を追加した。**ただしこれはmacOS内部の挙動に基づく推測であり、完全な解決策として証明されたものではない**（OS側のLaunch Constraint機構の詳細はソースからは検証不能）。同種の不具合を踏んだ場合は、まず`launchctl bootout gui/$(id -u)/com.yukiorita.MLXBar.Coordinator`と`~/Library/LaunchAgents/com.yukiorita.MLXBar.Coordinator.plist`の削除を試すこと。
- 開発中に複数のビルド（debugバイナリ、`dist/MLXBar.app`、`/Applications/MLXBar.app`等）を同じMacで行き来してテストすると、この競合を自分で引き起こしやすい。テスト後は`mlxbarctl remove-all-data --yes`または上記の手動bootoutでクリーンな状態に戻すこと。

### Coordinator側の`__version__`が2バージョン以上古いまま放置されていた（v1.3.0で発見・修正）
- `Coordinator/mlxbar/__init__.py`の`__version__`が`"1.1.0"`のまま、v1.2.0〜v1.2.4のリリースを通じて一度も更新されていなかった。`/api/v1/health`はこの値を返す。
- `CoordinatorClient.isExpectedVersionHealthy()`（Swift側）は、`/api/v1/health`が返す`version`とアプリ本体の`CFBundleShortVersionString`が**完全一致**することを健全性の条件にしている。バージョンが食い違ったままだと、この一致チェックは**恒久的に失敗し続ける**。
- 実害: `startService()`は「バージョン一致した健全な応答」が得られるまで`installFallbackLaunchAgent()`（bootout→bootstrap→kickstart）を経由し続ける設計のため、Coordinator自体は正常に動いていても、**アプリを起動するたびに毎回LaunchAgentの登録を丸ごと作り直していた**可能性が高い。GUIの「サービス稼働中」表示（`refreshStatus()`が直接叩く`/api/v1/status`）はこのチェックを経由しないため気付きにくい。
- v1.3.0で発見した本件の教訓を踏まえると、A2で対策した「Launch Constraint Violation」自体も、実は**この不要な毎回の再登録サイクルが遠因だった可能性がある**（登録を作り直す回数が多いほど、ad-hoc署名のIDが変わるビルドの入れ替わりと衝突する機会も増える）。今後、同種の不可解な起動不安定を調査する際は、まず`curl --unix-socket .../coordinator.sock http://mlxbar/api/v1/health`で返る`version`とアプリの実バージョンが一致しているか確認すること。

### Coordinatorのクラッシュログ（v1.3.0で追加）
- launchdの設定ファイルはセキュリティ上の理由（共有`/tmp`のシンボリックリンク攻撃を避けるため）で`StandardErrorPath`をあえて設定していない。そのため、`logging`モジュールが初期化される前の例外や、どこにも捕捉されない例外は、以前は完全に失われていた。
- `main.py`の`run()`に、`logging`の状態に依存しない緊急ログ書き込み（`_emergency_log`）を追加。書き込み先は`~/Library/Application Support/MLXBar/logs/coordinator-crash.log`。SIGKILLはOSレベルで捕捉不可能なため対象外（これは物理的な制約であり、コードでは解決できない）。

### `POST /api/v1/system/reset`のシャットダウン順序（v1.3.0で追加）
- 「すべてのデータを削除」の実体（設定・トークン・DB・ランタイム・uvキャッシュ・workerソケットの削除）はCoordinator自身が持つ`AppState.reset_all()`に一本化した（`state.py`）。以前はSwift側がハードコードしたパスリストで削除しており、`MLXBAR_HOME`環境変数でrootを変えた場合に理論上ずれる余地があった。
- 実装上、**レスポンスを返すより前にプロセスを終了させてはいけない**。`reset_all()`は同期的に（ジョブキャンセル→DB接続クローズ→ログハンドラクローズ→`control/coordinator.sock`以外の削除）まで行った後、`state.management_server.should_exit = True`をセットするだけでリクエストハンドラを返す。実際のシャットダウン処理（`serve()`の`finally`節）は、uvicornがこのHTTPレスポンスを書き終えた後の次のイベントループサイクルで走る。**`os.kill(os.getpid(), signal.SIGTERM)`による自己シグナル送信は、リクエストハンドラ自身が実行中のイベントループに対して確実に非同期処理されるとは限らず、実機テストで`finally`節が実行されないまま謎のプロセス終了が起きることを確認した**ため採用していない。`management`（uvicornの`Server`インスタンス）を`AppState`に保持しておき、`should_exit`を直接立てる方式に変更した。
- ソケットファイル（`control/coordinator.sock`）は`reset_all()`では触らず、既存の`serve()`の`finally`節の`socket_path.unlink(missing_ok=True)`に完全に委譲する。理由: レスポンスを返す時点でリスナーがまだこのソケットを握っているため。
- **この機能を実装中に、無関係な既存の起動レースを発見・修正した**: `state.install_missing_runtimes()`（自動ランタイムインストールのジョブ登録）が、管理APIがリクエストを受け付け可能になった**後**に呼ばれていたため、起動直後の数十〜百ミリ秒の間に外部からリクエスト（特に`/system/reset`のようにDBを閉じる処理）が届くと、起動処理自体が「閉じたDBへの書き込み」でクラッシュすることがあった（結合テストで実際に再現・確認）。`install_missing_runtimes()`とその後のスキャン判定を、管理APIサーバー（`management`のuvicorn起動）より前に移動して解決した。

### CLIとGUIの機能パリティにおけるOS API境界（v1.3.0で対応）
- `mlxbarctl`（Python）はGUIのほぼ全機能を操作できるようにしたが、2点だけmacOSのAPI境界により完全な等価にはできない:
  - **ログイン時に起動**: `SMAppService`はSwift/ObjC専用のFoundation APIで、Pythonから直接呼べない。CLIは`general.launchAtLogin`という設定値だけを変更でき、実際のOS登録はGUIアプリが起動・設定再取得したタイミングで`MenuBarViewModel.reconcileLaunchAtLogin()`が反映する（次にGUIが起動するまでは希望状態と実際の登録がずれる）。
  - **`remove-all-data`のログイン項目解除**: 同じ理由で`SMAppService.unregister()`をCLIから呼べない。`launchctl bootout`（Pythonの`subprocess`から実行可能）でサービス自体は確実に停止するため実害はなく、システム設定のログイン項目に見た目上のエントリが残るだけ（アプリ本体を削除すれば消える）。
- 逆に言うと、`launchctl`・`defaults`はただのコマンドラインツールなのでPythonからも`subprocess`で問題なく呼べる。SwiftでしかできないのはFoundationの`ServiceManagement`フレームワーク呼び出しだけ、という切り分けを覚えておくこと。

## リリース時のバージョン文字列更新チェックリスト

新バージョンをリリースする際、以下のファイルの版数表記をすべて更新すること（漏れやすい）:

- `Coordinator/pyproject.toml`（`version`）
- `Coordinator/mlxbar/__init__.py`（`__version__`） — ★ `/api/v1/health`が返す値。**v1.1.0からv1.3.0まで更新漏れしていたことをv1.3.0で発見**（下記参照）。今後は特に注意。
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
