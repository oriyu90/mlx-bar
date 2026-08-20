# mlx-bar 保守メモ

> 公開物（README・紹介サイト等）には出さない、次回以降の開発向けメモ。
> [common-rules-document](https://github.com/oriyu90/common-rules-document/blob/main/common%20rules.md) ルール6に基づき作成。

## 未解決の課題

### ZCodeのlarge tool prefixによるcold prefill遅延（2026-08-21対応）
- 実機証拠: v1.3.6の直近ZCode要求は`stream=true`、messages 4件、tools 23件、HTTP 200で33.124秒。制御実験では同じQwenをthinking無効・toolsなしでfirst content 0.946秒、23 tools付きで7.506秒となり、thinking強度ではなくtool promptのprefillが差の中心だった。
- 根本原因: v1.3.6は生成開始後の全量バッファを解消したが、各OpenAI Chat Completions要求を独立生成として`mlx_vlm.stream_generate`へ渡し、ZCodeが毎ターン再送するsystem prompt・tools schema・履歴の同一prefixも毎回先頭から計算していた。27B 8bitの実tool schemaは大きく、first token前のcold prefill中はSSE keep-aliveしか返せない。
- 修正: mlx-vlm 0.6.15公式の`PromptCacheState`をWorkerのモデル寿命に合わせて1個保持し、テキスト要求の最長共通token prefixを再利用する。キャッシュは直前系列1件だけなので無制限に増えない。OpenAI入力のmessages/toolsやモデル出力は変更しない。
- 安全境界: 画像のplaceholder tokenは画像内容の同一性を保証しないため画像要求へキャッシュを渡さない。生成キャンセル・例外時はin-place更新された途中キャッシュを破棄する。キャッシュ保持でメモリ比率が安全上限へ達した場合は`clear_prompt_cache` RPCで解放後に再測定し、それでも高い場合だけ`MEMORY_PRESSURE`を返す。古いmlx-vlmに`PromptCacheState`がない場合は最適化なしで継続する。
- 可観測性: APIログへ本文・tool定義・キーを保存せず、文字数、first-token時間、prompt/cached tokens、prompt/generation TPS、推論モードだけを追加。既存SQLiteには追加列を自動migrationする。
- 推論強度dialect: 汎用テンプレートとの互換性を保つためOpenAIの`high` / `minimal`を原値で先にrenderし、失敗時だけQwen相当の`xhigh` / `low`を再試行する。明示された`none`は従来どおりthinking無効化へ変換する。
- 実モデル評価: 23 tools・55,881 schema文字・10,677 prompt tokensのcold要求はfirst delta 35.253秒／total 35.299秒。会話を1ターン延長したwarm要求は10,688 cached tokens、first delta 0.399秒／total 0.406秒、prompt TPS 308.92→36,911.43で約88倍のTTFT改善。モデルロード直後や共通prefixがない最初の要求は原理上cold prefillが残る。

### ZCode tool calling時の無表示・全量バッファ（2026-08-21対応）
- 実機証拠: v1.3.5のZCode要求はHTTP 400ではなく200になったが、`stream: true`、ツール20件、会話履歴最大236件で、完了まで42〜519秒を要した。Workerクラッシュも1回記録。最小のQwen＋thinking＋1 tool要求でも、空の初期チャンクとkeep-aliveだけが55秒続き、本文が完了時に一括到着した。
- 原因: [server.py](Workers/common/server.py)がtools有効時の全`delta`を`buffered`へ追加し、生成完了後のtool parserまで外部へ出していなかった。クイックチャットはtoolsなしのため逐次配信され、症状差と一致する。
- 修正: 増分フィルターが通常本文とreasoningを即時イベント化し、分割され得る制御タグの短い接尾辞だけを保留する。`<tool_call>`を検出した時点から解析用に保持し、確定したcallだけをOpenAI形式へ変換する。thinkingは`reasoning_content`へ分離し、解析失敗は明示エラーにする。
- 署名: runtime Pythonが同梱Workerソースへ`__pycache__`を作り、起動後にsealed resource追加として署名検証を壊していた。全Coordinator/Worker起動経路へ`PYTHONDONTWRITEBYTECODE=1`を設定する。
- 検証: 実機で観測したQwenのthinking終端とtool markerの分割パターンを含む回帰テストを追加し、Pythonテスト148件とSwiftリリースビルドが成功。

### ZCode互換拡張の将来互換化（2026-08-21対応）
- 症状: v1.3.4でトップレベルの`thinking`を追加した後も、実機のZCodeからQwen3.8へ送った要求がHTTP 400 `UNSUPPORTED_PARAMETER`になった。インストール済みアプリと配布DMGのCoordinatorハッシュは一致しており、v1.3.4未適用ではなかった。
- 原因: OpenAI互換入口、`extra_body`、`thinking`の各階層に未知項目を拒否するホワイトリストが残っており、ZCodeのバージョンやモデル別dialectによる追加項目で再発できる設計だった。旧監査ログは早期入力エラーのモデル名とエラーコードも記録できず、ZCode側も`parameters`配列を表示しないため、実機の拒否項目名を後から特定できなかった。
- 修正: 未知の互換拡張はWorkerへ渡さず無視し、既知の値だけを検証・正規化する。`extra_body.thinking`、`extra_body.reasoning_effort`、`thinking.effort`も受理する。MLXBar自身が管理する予約済み`chat_template_kwargs`は引き続き拒否する。
- 診断: API処理の先頭で本文を含まない監査メタデータを作り、HTTP入力エラーのモデル名とコードをSQLiteへ、拒否項目名だけをCoordinatorログへ残す。会話本文、応答本文、APIキーは保存しない。
- 検証: ZCode互換拡張と診断ログの回帰テストを追加し、Pythonテスト144件が成功。

### ZCodeのトップレベル`thinking`・`reasoning_effort`対応（2026-08-21対応）
- 症状: v1.3.3で`extra_body.chat_template_kwargs`に対応した後も、ZCodeがQwen3.8の要求にトップレベルの`thinking`を追加すると、Coordinatorのホワイトリスト検証がHTTP 400 `UNSUPPORTED_PARAMETER`を返していた。さらに`reasoning_effort`は許可項目にあるが、生成`options`へ入れておらず実質的に無視していた。
- 修正: `thinking`をブール値またはオブジェクトとして検証し、`type: enabled|adaptive`を`enable_thinking: true`、`type: disabled`を`false`、`budget_tokens`を`thinking_budget`、`clear_thinking`を逆値の`preserve_thinking`へ変換。トップレベルの`reasoning_effort`も小文字化して同名のテンプレート引数へ渡し、`none`の場合は明示値がない限りthinkingを無効化する。
- 優先順位: モデルに直結する既存の`extra_body.chat_template_kwargs`を最優先し、トップレベルの`thinking`と`reasoning_effort`は未指定値だけを補う。ZCodeが新旧両形式を同時に送っても、明示的なテンプレート設定が意図せず上書きされない。
- 検証: ZCode形式の正規化、明示値の優先、`reasoning_effort: none`、不正なtype・budget・履歴保持値を追加し、Pythonテスト142件が成功。

### ZCodeの`extra_body.chat_template_kwargs`対応（2026-08-20対応）
- 症状: ZCode 3.2.5が`POST /v1/chat/completions`に`{"extra_body":{"chat_template_kwargs":{"enable_thinking":false}}}`を追加すると、Coordinatorが`extra_body`を未対応パラメータと判定してHTTP 400 `UNSUPPORTED_PARAMETER`を返していた。モデルロードや量子化、生成Workerに到達する前のAPI入力検証が原因。
- 修正: [openai_compat.py](Coordinator/mlxbar/api/openai_compat.py)で`extra_body.chat_template_kwargs`を検証・抽出し、Worker生成オプションへ追加。[tool_calls.py](Workers/common/tool_calls.py)のテンプレート引数フォールバックの各段階で値を保持し、mlx-lmの`processor.apply_chat_template`とmlx-vlmの`prompt_utils.apply_chat_template`の両方へ届ける。
- 境界: `chat_template_kwargs`はモデル固有拡張のため任意のJSON値を伝播するが、MLXBarのAPI処理と衝突する`tools`、`tool_choice`、`tokenize`、`add_generation_prompt`、`num_images`は予約済みとして拒否する。`extra_body`内の未対応項目も従来の厳密なAPI検証に合わせて拒否する。LM Studio経路はMLXテンプレートを使わないため伝播対象外。
- 検証: API正常系・不正系、両Workerへの伝播、tool callingフォールバック時の保持をテストし、Pythonテスト138件が成功。Swiftは変更していないが、互換SDKを明示したリリースビルドも成功。

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

### メニューバーを開くと即座にクラッシュしていた問題（`Bundle.module`の資材解決ミスマッチ、v1.1.0から潜在・v1.3.0で発見）
- 症状: メニューバーのアイコンをクリックすると、ポップアップが開く前に一瞬で消える（プロセスごと終了する）。v1.2.4の「アイコンがフラッシュして消える」報告と見た目は同じだが**原因は別**（v1.2.4は`refreshStatus()`の毎秒再描画によるちらつき、こちらは`fatalError`によるプロセス終了）。
- 根本原因: [Localization.swift](Sources/MLXBar/Services/Localization.swift)の`AppLanguage.bundle(for:)`が、SwiftPM自動生成の`Bundle.module`アクセサ経由でローカライズ資材（`MLXBar_MLXBar.bundle`）を読んでいた。このアクセサ（executable target向け生成コード）は`Bundle.main.bundleURL`（`.app`直下）を探すが、`scripts/build-release.sh`は同バンドルを`Contents/Resources/`配下に配置している（[CoordinatorClient.swift:329](Sources/MLXBar/Services/CoordinatorClient.swift:329)の`bundleResources`もこちらの規約に従う）。探索先と実配置がずれており、`resource_bundle_accessor.swift`生成コードの`fatalError("could not load resource bundle: ...")`でプロセスごと終了していた。
- 発火条件: `AppLanguage.text(_:language:)`は`current == sourceLanguage`（`"ja"`）のとき`Bundle.module`に触れず即returnするガードがある。`guiLanguage`はCoordinatorの`general.language`設定が無ければ`"en"`がデフォルト（[MenuBarViewModel.swift:502](Sources/MLXBar/MenuBar/MenuBarViewModel.swift:502)）のため、**新規インストール・英語UIのユーザーは高確率で即クラッシュする**一方、GUI言語を日本語で使うことが多い開発環境では踏みにくく、実機検証をすり抜けていたと考えられる。`AppLanguage`・`guiLanguage`はどちらもv1.1.0で導入されており、この構造自体はそこから潜在していた可能性が高い。
- 修正: `AppLanguage`に`resourceBundle`という static let を追加し、`CoordinatorClient`と同じ`Contents/Resources/MLXBar_MLXBar.bundle`パスを最初に試し、見つからない場合（`swift run`でのローカル実行など`.app`化されていない場合）のみ`Bundle.module`にフォールバックするようにした。
- 教訓: 同一コードベース内でリソースバンドルの場所を解決するロジックが2系統（SwiftPMの`Bundle.module`規約 と、`CoordinatorClient`が使う手動`Contents/Resources`パス構築）併存していたのが根本原因。**今後`Bundle.module`を新規に使う箇所を追加する前に、`CoordinatorClient.swift`の`bundleResources`と同じ手動解決に統一できないか検討すること。**

### mlx-vlmモデルでの生成が`apply_chat_template requires jinja2`で失敗していた問題（v1.3.2で発見・修正）
- 症状: ZCode（外部のOpenAI互換クライアント）からmlx-vlmモデル（画像対応、モデル名`Laguna-S-2.1-oQ2e`）へ生成リクエストを送ると、`GENERATION_FAILED` / `apply_chat_template requires jinja2 to be installed. Please install it using pip install jinja2.`で失敗する。mlx-lmモデル（テキストのみ）では発生しない。
- 根本原因: `apply_chat_template`は`mlx_vlm.prompt_utils.apply_chat_template`（[Workers/mlx_vlm_worker/adapter.py:43](Workers/mlx_vlm_worker/adapter.py:43)）経由で最終的にHugging Face `transformers`のtokenizer/processorが担当するが、`transformers`自体は`jinja2`を必須依存として宣言していない（オプション扱い）。ランタイムのインストール処理（[Coordinator/mlxbar/runtimes/updater.py](Coordinator/mlxbar/runtimes/updater.py)の`stage()`）は`uv pip install --python <venv> <engine> fastapi uvicorn`のみを指定しており、`jinja2`を明示していなかった。
- 実機の`~/Library/Application Support/MLXBar/runtimes/{mlx-lm,mlx-vlm}/slots/*/requirements.lock`を比較して確定: **`mlx-lm`側はたまたま別の依存経由で`jinja2==3.1.6`が入っていた**（`mlx-vlm`側の依存グラフには`mlx-audio`・`opencv-python`など`mlx-lm`にはない多数のパッケージが入っており、逆に`mlx-lm`側にだけ`jinja2`/`markupsafe`が入っている非対称な状態だった）一方、**`mlx-vlm`側には`jinja2`もその依存の`markupsafe`も一切含まれていなかった**。`.venv`のsite-packagesを直接確認しても`mlx-vlm`スロットにだけ`jinja2`ディレクトリが存在しないことを確認済み。
- 修正: `updater.py`の`uv pip install`コマンドに`jinja2>=3.1,<4`を明示追加。両エンジンとも今後は依存解決の巡り合わせに関係なく確実にインストールされる。
- 注意（重要）: **この修正はインストーラーのコードのみを直しており、既にインストール済みのランタイムスロットは自動的には直らない。** 影響を受けたユーザーは、アップデート後にランタイム画面（または`mlxbarctl runtime`）からmlx-vlmランタイムを再インストール・更新して新しいslotを作らせる必要がある。このMac自体は応急処置として、既存slotの`.venv`へ`uv pip install --python <venv>/bin/python jinja2`を直接実行し、`requirements.lock`も`uv pip freeze`で更新して整合させた上でCoordinatorを`launchctl kickstart -k`で再起動して即時復旧させた。
- 教訓: `uv pip install`で「そのパッケージが動くのに必要な全依存」を、宣言されたものだけに頼って揃えようとすると、あるライブラリ（ここでは`transformers`）がオプション扱いにしている実行時必須の機能（`apply_chat_template`のjinja2レンダリング）が、たまたま別経路で入るかどうかに左右されてしまう。**両エンジンに共通で必要な「暗黙の実行時依存」がないか、他のアダプター（[Workers/common](Workers/common)）の使い方も含めて棚卸しする価値がある。**

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
