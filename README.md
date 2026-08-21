# MLXBar

Version 1.4.1 — repository: [oriyu90/mlx-bar](https://github.com/oriyu90/mlx-bar)

MLXBarは、Apple Silicon Mac上のMLX LM、MLX VLM、LM Studioモデルをメニューバーから一元管理するmacOSアプリです。GUI、`mlxbarctl`、OpenAI互換APIが同じバックエンド状態を共有します。APIは既定でこのMacだけに公開され、明示的に有効化した場合だけローカルネットワークから接続できます。

GUIの標準言語はEnglishです。「Settings…」→「General」→「Language」で「日本語」へ切り替えられます。選択は保存され、次回起動時も維持されます。

## 主な機能

- MLX LM / MLX VLM / LM Studioモデルの統合カタログ
- MLXモデルのロード、ストリーミング生成、キャンセル、アンロード
- MLX VLMへの画像入力
- Lagunaなど、mlx-vlmが提供するテキスト専用アーキテクチャへの対応
- mlx-lm非対応モデルをmlx-vlmで自動再試行
- GGUFモデルのLM Studio Provider経由利用
- モデルフォルダの追加と再スキャン
- OpenAI互換のモデル一覧・モデル詳細・Chat Completions API（Open Interpreter、LibreChat、ZCode向け）
- API要求で指定されたモデルの自動ロードと、アプリ／Worker再起動後の自動復元
- 長いZCode入力やtool calling解析中も接続を維持するストリームheartbeat
- ZCodeの並列subagent要求を到着順に処理する生成キュー
- mlx-vlmの長いZCode prefixを再起動後も再利用する、容量制限付き永続プロンプトキャッシュ
- ZCode等のOpenAI Chat Completionsクライアント向けtool calling（履歴、`tools`、`tool_choice`、ストリーミング差分）
- 本文とAPIキーを含めない、最大2,000件の最近のAPIログ
- モデルから検出したトークン上限の表示、ユーザー上限設定、超過要求の自動調整
- 温度・Top P・繰り返しペナルティの既定値設定とAPI別上書き
- モデルコンテキストに応じた長いZCode会話履歴と、メニューバーの応答中／待機中表示
- 既定localhost限定、任意のLAN公開、APIトークン認証、秘密情報を除いた診断情報
- 公開APIポートの検査と、旧接続をdrainするデュアルリスナー切替
- MLX LM / MLX VLMを分離したランタイムの初回自動インストール、更新、検証、復元、旧版削除
- GUIと同じ管理APIを利用する`mlxbarctl`

## 動作要件

- Apple Silicon Mac
- macOS 14以降（macOS 15以降を推奨）
- LM Studio Providerを使う場合はLM Studioと`lms` CLI
- モデル容量に応じた空きストレージとユニファイドメモリ

## インストール

1. [GitHub Releases](https://github.com/oriyu90/mlx-bar/releases)から`MLXBar-1.4.1.dmg`をダウンロードして開きます。
2. `MLXBar.app`を`Applications`へコピーします。
3. 初回起動時にmacOSの確認が表示された場合は、「システム設定」→「プライバシーとセキュリティ」から起動を許可します。
4. 初回起動時に`mlx-lm`と`mlx-vlm`がない場合は、両ランタイムをバックグラウンドで自動インストールします。「Settings…」→「Runtime」で進捗やエラーを確認できます。
5. 「Choose Model…」（日本語UIでは「モデルを選択…」）からモデルをロードします。

Developer ID署名・公証済みの正式配布版では、手順3は通常不要です。ローカルビルドは利用可能な署名証明書がない場合にad-hoc署名されます。

## 初期設定と保存先

状態は次の場所に保存されます。

```text
~/Library/Application Support/MLXBar/
├── config.json
├── state.sqlite3
├── control/coordinator.sock
├── control/api-token
├── prompt-cache/
├── runtimes/
└── logs/
```

公開APIは初期状態で`http://127.0.0.1:11435`です。APIトークンは設定JSONへ保存せず、ユーザーだけが読める管理ファイルへ保存します。LAN公開とremote code実行は初期状態で無効です。LAN公開を有効にするとAPIキーは必須になり、設定画面へ別PC用の接続URLが表示されます。

## GUIの使い方

- 「モデルを選択…」: 検索、ソース、形式、エンジンを確認してロード
- 「クイックチャット…」: テキスト生成と、VLM選択時の画像入力
- 「今すぐ再スキャン」: Hugging Faceキャッシュ、LM Studio、追加フォルダを再走査
- 「フォルダを選択…」: 通常のフォルダに加え、`~/Library`と`/Library`を専用メニューから直接選択（隠しフォルダも表示）
- 「モデル」設定: API自動ロード、Max token上限、温度・Top P・繰り返しペナルティ、並列リクエストを設定・確認
- 「一般」設定: GUI言語をEnglish／日本語から選択
- 「APIサーバー」: URL確認、コピー、ポート変更
- 「詳細」: 最近のAPIアクセスを最大500件表示、コピー、消去
- 「ランタイム」設定: 自動インストール状況の確認、最新版または指定版への更新、検証後の切替・復元・旧版削除
- 「キャッシュ」設定: 永続プロンプトキャッシュの有効化、容量上限、使用量確認、RAM／ディスクキャッシュの個別消去

モデルのロード中は、対象モデル名、エンジン、現在段階、経過秒数をモデル画面とメニューバーに表示します。完了後は「モデル名をコピー」またはメニューバーのコピーボタンから、読み込み済みモデル名をクリップボードへコピーできます。

メニューバーを開いている間は1秒間隔で状態を更新します。ZCodeやLAN内の別PCからリクエストを処理している間は「モデルが応答を生成中」、並列要求がある場合は待機件数、生成要求がなくロード済みの場合は「待機中」と表示します。外部API経由の生成でもメニューバーアイコンと表示が切り替わります。

Lagunaのように画像入力を持たないモデルでも、`mlx-vlm`側に専用実装がある場合は`mlx-vlm`へ分類します。また、`mlx-lm`で互換性エラーになったローカルモデルは`mlx-vlm`で一度だけ自動再試行します。画像追加ボタンは、ロード結果が画像入力対応を明示した場合だけ有効になります。

### Max token上限

「設定…」→「モデル」の「Max token上限」で、1〜2,000,000 tokensの範囲を設定できます。MLXBarはローカルモデルの`config.json`などからコンテキスト上限を検出し、「設定した上限」と「モデルから検出した上限」の小さい方をAPI有効上限として使用します。現在値はモデル設定、モデル一覧のロード状態、メニューバーに表示されます。

ZCodeなどがAPI有効上限より大きい`max_tokens`または`max_completion_tokens`を送った場合、400エラーにはせず有効上限まで自動的に下げます。0、負数、数値でない値は入力エラーです。上限を大きくすると生成時間とメモリ使用量が増えるため、モデルとMacのメモリ量に合わせて設定してください。既定値は8,192です。

ZCodeのシステム指示、会話履歴、ツール結果が従来の100,000文字を超える場合、ロード中モデルのコンテキスト上限を基に入力事前検査を自動拡張します。目安はモデル上限の4倍、かつ最大10,000,000文字です。Laguna-S-2.1-oQ2eでは4,194,304文字になります。文字数は安全な事前検査であり、正確なトークン化とコンテキスト判定はモデルランタイムが行います。

同じモデルフォルダが「追加フォルダ」とLM Studio既定フォルダの両方から見つかった場合は、正規化した実パスで1件へ統合します。`lms`とLM Studio APIから同じProviderモデルが返った場合もProviderキーで統合します。表示名が同じでも実パスが異なるモデルは別モデルとして保持されます。

### 既定の生成パラメータ

「設定…」→「モデル」→「既定の生成パラメータ」で次の値を保存できます。

- 温度（0〜2、既定0.7）: 0では決定的な出力になり、大きいほど候補の選択が多様になります。
- Top P（0〜1、既定1.0）: 小さくすると累積確率の高い候補へ絞ります。
- 繰り返しペナルティ（0.01〜2、既定1.0）: 1.0で無効、1より大きいほど同じトークンの繰り返しを抑えます。
- ペナルティ対象範囲（1〜32,768 tokens、既定20）: 直近の何トークンを繰り返し判定に使うかを指定します。

mlx-lmとmlx-vlmでは4項目すべてをランタイムへ渡します。LM Studio経由ではOpenAI互換性が確認できる温度とTop Pを渡します。クイックチャットは保存した温度から開始し、Top Pと繰り返しペナルティは保存値を使用します。ZCode、LibreChatなどが`temperature`、`top_p`、`repetition_penalty`、`repetition_context_size`を要求ごとに指定した場合は、その要求値を優先します。不正な範囲はモデルを実行せず入力エラーとして返します。

### MLXランタイムの自動更新

「設定…」→「ランタイム」では、`mlx-lm`と`mlx-vlm`を個別に更新できます。未インストールのランタイムはアプリ起動時に自動インストールされます。「最新版へ自動更新」を押すと、以降は次の処理を自動で行います。

1. 公式PyPIから最新安定版を確認
2. 現在の環境を変更せず、新しい保存領域へダウンロード
3. 依存関係、import、ストリーミングAPI契約を検証
4. 実行中の生成があれば完了を待機
5. 新しい環境へ安全に切り替え、ワーカーの動作を確認
6. 更新前にモデルがロードされていた場合は再ロード
7. 切替後の失敗時は以前の環境へ自動復帰

更新履歴、現在の処理段階、進捗率、ダウンロード中の経過時間は同じ画面に表示されます。画面を閉じたりアプリのGUIを再起動したりしても、実行中の更新へ再接続して表示を続けます。「処理を中止」ではダウンロード用プロセスも停止し、作成途中の環境を片付けます。

各ランタイムは独立しており、`mlx-lm`の更新で`mlx-vlm`の環境は変更されません。既定では現用・直前・予備を最大3件保持します。「詳細・手動操作」から、使用中ではない以前のランタイムを削除できます。復元先の版を削除すると、その版への復元はできなくなるため確認画面を表示します。使用中の版と更新処理中の版は削除できません。

ランタイム管理画面を開くと、PyPIから`mlx-lm`と`mlx-vlm`の最新安定版をキャッシュなしで取得します。正式なバージョン規則でインストール済み版と比較し、「最新版です」「更新できます」「最新版をインストールできます」「安定版より新しい版を使用中です」のいずれかを表示します。確認日時も保存され、手動の「更新を確認」から再取得できます。

GGUFはMLXワーカーへ渡されず、LM Studio Providerだけが選択されます。LM Studio自身がロードしているモデルはMLXBarの「全体で1モデル」制約の対象外なので、同時利用時はメモリ量に注意してください。

### 生成の安全機能

- 生成本体はメモリ保護のため同時に1件だけ実行し、並列要求は最大16件までFIFOキューで待機します。
- 待機中は10秒間隔で接続を維持し、既定の最大待ち時間は3,600秒です。満杯時だけ再試行可能なHTTP 429を返します。
- GUIの「停止」は協調停止を要求し、5秒以内に終わらない場合はMLX WorkerまたはLM Studio接続を強制終了します。「すべての生成を停止」では待機中の要求も取り消します。
- モデルロードは最大10分、生成は最大15分で停止します。長い入力処理中は10秒間隔で生存通知を送ります。
- `max_tokens`は最大8192、promptは最大100,000文字、画像は最大8件・1件25MBです。
- MLXのactive/cacheメモリが物理メモリの90%に達した場合は、新しい生成を開始しません。
- Worker通信が切断された場合はモデル状態を解除し、再ロード可能な状態へ復帰します。

これらの既定値は`~/Library/Application Support/MLXBar/config.json`の`generation`項目に保存されます。上限を緩和するとメモリ不足や長時間無応答の危険が増えるため、通常は変更しないでください。

## CLI

アプリ内の`mlxbarctl`はGUIと同じUNIX Domain Socket管理APIを使います。

```sh
mlxbarctl status --json
mlxbarctl model list --json
mlxbarctl model scan --wait
mlxbarctl model load MODEL_ID --engine auto
mlxbarctl model add-folder /path/to/models
mlxbarctl model remove-folder /path/to/models
mlxbarctl generate --prompt "こんにちは"
mlxbarctl generate --prompt "こんにちは" --temperature 0.4 --top-p 0.9 --repetition-penalty 1.1 --repetition-context-size 64
mlxbarctl model unload
mlxbarctl cancel-all
mlxbarctl runtime list
mlxbarctl runtime stage mlx-lm --version VERSION --wait
mlxbarctl runtime update mlx-lm --wait
mlxbarctl runtime update mlx-vlm --wait
mlxbarctl runtime activate mlx-lm SLOT_ID
mlxbarctl runtime rollback mlx-lm
mlxbarctl runtime delete-slot mlx-lm SLOT_ID
mlxbarctl runtime cancel-job mlx-lm
mlxbarctl config get
mlxbarctl config set api.port 12000
mlxbarctl config set-language ja
mlxbarctl config set-max-tokens 8192
mlxbarctl config set-queue-limits --max-queued 16 --timeout-seconds 3600
mlxbarctl config set-sampling-defaults --temperature 0.7 --top-p 1.0 --repetition-penalty 1.0 --repetition-context-size 20
mlxbarctl config set-launch-at-login true  # デスクトップのGUIが次に起動したときにOS登録へ反映されます
mlxbarctl secrets get-api-token
mlxbarctl secrets regenerate-api-token
mlxbarctl secrets set-lmstudio-token TOKEN
mlxbarctl secrets set-lmstudio-token  # 引数省略で削除
mlxbarctl logs show --limit 100
mlxbarctl logs clear
mlxbarctl network set-lan --enabled
mlxbarctl network set-lan --disabled
mlxbarctl network set-port 12000
mlxbarctl api test-port 12000
mlxbarctl diagnostics
mlxbarctl remove-all-data --yes  # 設定・APIキー・モデルDB・ランタイム・ログを削除してサービスを停止します
```

終了コードは、0=成功、2=入力不正、3=未起動、4=競合、5=互換性なし、6=更新失敗、7=認証失敗、8=内部エラーです。

GUIで操作できることはほぼすべて`mlxbarctl`からも操作できます。唯一「ログイン時に起動」だけは、macOSのログイン項目登録API(`SMAppService`)がSwift専用のためCLIから直接は変更できず、CLIは設定値だけを変更します。実際のOS登録は、次にGUIアプリが起動または設定を再取得したタイミングで反映されます。

## OpenAI互換API

トークンを指定して利用します。

```sh
curl http://127.0.0.1:11435/v1/models \
  -H "Authorization: Bearer YOUR_TOKEN"

curl http://127.0.0.1:11435/v1/chat/completions \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "loaded",
    "messages": [{"role": "user", "content": "こんにちは"}],
    "stream": true
  }'
```

管理API、モデルの実パス、更新操作は公開TCP APIへ露出しません。

APIのモデル指定には、`GET /v1/models`が返す表示名または内部IDを利用できます。モデルが未ロードでも、既定では最初の要求時にカタログから探してロード完了を待ち、その後に生成します。アプリやモデルWorkerの再起動後も同様に復元します。「設定…」→「モデル」で自動ロードを無効にできます。メニューバーから明示的にアンロードした場合は、意図しない再起動を防ぐためGUIで再ロードするまで自動ロードしません。

OpenAI互換エラーはトップレベルの`error`オブジェクトで返します。一般的なクライアントが付加する`top_p`、penalty、`seed`、`response_format`、`metadata`、`store`なども受理します。応答には`usage`を含め、`stream_options.include_usage: true`では`[DONE]`の直前にusage専用チャンクを返します。複数候補生成（`n > 1`）とテキスト以外の出力は未対応で、フリーズせず入力エラーとして終了します。

ZCodeが送る`extra_body.chat_template_kwargs`に加え、トップレベルまたは`extra_body`内の`thinking`と`reasoning_effort`も受理し、mlx-lm・mlx-vlmのチャットテンプレートへ渡します。`thinking.type`の`enabled` / `disabled`は`enable_thinking`へ、`budget_tokens`は`thinking_budget`へ、`clear_thinking`は逆値の`preserve_thinking`へ、`thinking.effort`は`reasoning_effort`へ変換します。将来のZCodeやOpenAI互換クライアントが追加する未知の拡張項目は生成へ渡さず安全に無視するため、項目追加だけでHTTP 400になりません。同じ値が`extra_body.chat_template_kwargs`に明示された場合はそちらを優先します。`tools`、`tool_choice`、`tokenize`、`add_generation_prompt`、`num_images`はMLXBarが管理するため、`chat_template_kwargs`内での上書きは受け付けません。

tool calling有効時も通常本文を生成中に逐次配信します。Qwen等のthinking部分はストリームの`delta.reasoning_content`へ分離し、`<think>`や`<tool_call>`の内部マークアップを通常本文へ漏らしません。tool callの開始後だけ解析用に保持してOpenAI形式の`delta.tool_calls`へ変換します。解析不能なtool callは無言で終了せず`TOOL_PARSE_FAILED`を返します。

v1.4.1では、ZCodeやGUIがSSE接続を途中で閉じても内側の生成処理を明示的に終了します。生成ロックは要求ID付きで所有され、所有者が実行中にも待機中にも存在しない孤立状態だけをGUI状態更新やキューheartbeatが自動回復します。正常なキュー移行や別要求のロックは解放しないため、モデル生成の直列性を維持します。診断情報の`generationLockState`と`generationLockRecoveries`で回復状態を確認できます。

mlx-vlmのテキスト要求では、直前要求との最長共通token prefixを安全に再利用します。ZCodeが毎ターン送る大きなsystem prompt・tools定義・会話履歴を再計算せず、OpenAI形式のmessages・tools・tool calling動作は変更しません。画像内容はtokenだけでは同一性を確認できないため画像要求にはキャッシュを共有せず、キャンセル・生成失敗時は途中キャッシュを破棄します。メモリ安全上限に達した場合はキャッシュだけを解放して再判定します。モデルロード後の最初の要求や共通prefixがない要求は従来どおりcold prefillが必要です。

v1.4.0では、mlx-vlm公式の`APCManager` / `DiskBlockStore`をディスク専用の下位層として追加しました。現在の`PromptCacheState`は高速なRAM層として維持され、Worker再起動後は`~/Library/Application Support/MLXBar/prompt-cache/`から共通prefixを復元します。モデル、tokenizer/chat template、重み、mlx-vlmランタイム版が変わると別namespaceになるため、互換性のないcacheを読みません。hybrid exact-cacheでは末尾256 tokensを毎回再計算し、その前の長いsystem/tools prefixを異なる最初のユーザー文でも再利用できるようにします。画像要求は引き続き共有対象外です。

「設定…」→「キャッシュ」では永続cacheの有効/無効、1〜100 GBの容量上限、使用量・disk hit数を確認でき、RAMまたはdisk cacheを個別に消去できます。永続cacheはKV状態とtoken IDをローカルへ保存するため、MLXBar専用フォルダはユーザーだけがアクセスできる権限で作成します。「すべてのデータを削除して終了」でも削除されます。APCの初期化・復元に失敗した場合、応答をまだ送信していなければv1.3.7の`PromptCacheState`/cold経路で一度だけ再試行します。OpenAI APIのmessages、tools、応答形式は変更しません。

APIアクセスログには本文・tool定義・APIキーを保存せず、`message_chars`、`tool_schema_chars`、`first_token_ms`、`prompt_tokens`、`cached_tokens`、`prompt_tps`、`generation_tps`、`cache_tier`、推論モードを記録します。長い初回応答が入力処理と生成のどちらに起因するかを切り分けられます。

OpenAI系クライアントの`reasoning_effort=high` / `minimal`は最初に原値でテンプレートへ渡し、Qwenが拒否した場合だけ同等の`xhigh` / `low`へ再試行します。汎用テンプレートのOpenAI表記を優先しながらQwenのdialectにも対応します。

v1.1.0のストリームは、全チャンクで同一の`id`と`created`を維持し、終了理由を持つチャンクを1回だけ送信します。長時間処理中はSSEコメントで接続を維持し、正常時は任意のusageチャンクの後に`data: [DONE]`で終了します。不正なJSON形状、`stream`、`stream_options`、token上限はOpenAI形式の`error`で返し、モデル実行前に終了します。

### Open Interpreter

Open Interpreter Classicでは、MLXBarのBase URL、APIキー、モデルを次のように指定できます。APIキー要求をOFFにした場合も、クライアント側で任意のダミー文字列が必要になることがあります。

```sh
interpreter --api_base "http://127.0.0.1:11435/v1" \
  --api_key "YOUR_TOKEN" \
  --model "openai/Laguna-S-2.1-oQ2e" \
  --llm_supports_functions
```

`openai/x`をモデル名として送る構成にも対応しています。この別名は最後にロードしたモデルへ解決されます。候補が1件だけの場合は、そのモデルを使用します。

### LibreChat

LibreChatの`librechat.yaml`ではCustom Endpointとして設定します。LibreChatをDockerで動かす場合、`127.0.0.1`はコンテナ自身を指すため、Mac上のMLXBarには`host.docker.internal`で接続します。

```yaml
endpoints:
  custom:
    - name: "MLXBar"
      apiKey: "${MLXBAR_API_KEY}"
      baseURL: "http://host.docker.internal:11435/v1"
      models:
        default: ["Laguna-S-2.1-oQ2e"]
        fetch: true
      titleConvo: true
      titleModel: "current_model"
```

Docker以外で同じMacから起動する場合は`http://127.0.0.1:11435/v1`、別PCからは設定画面に表示されるLAN用URLの末尾へ`/v1`を付けます。`models.fetch: true`では未ロードを含むMLXBarのモデルカタログを取得できます。

### ZCodeとtool calling

ZCodeではProvider形式を`OpenAI Chat Completions`、Base URLを`http://接続先:11435/v1`にします。ModelにはMLXBarでロードしたモデル名を指定し、APIキー要求が有効な場合は設定画面のAPIキーを指定します。別PCから使う場合は、先にMLXBarの「APIサーバー」でLAN公開を有効にしてください。

ZCode 3.2.5以降のモデル設定が追加する`extra_body.chat_template_kwargs`、`thinking`、`reasoning_effort`の各形式に対応しているため、Qwen3.8などでもOpenAI方式のままthinkingの有効化・無効化と推論強度を利用できます。設定変更後は新しい会話で接続を確認してください。

`POST /v1/chat/completions`は、`system`、`developer`、`user`、`assistant`、`tool`の会話履歴を順序どおりランタイムへ渡します。assistantの`tool_calls`とtoolの`tool_call_id`も次のターンまで保持します。`tools`、`tool_choice`（`none`、`auto`、`required`、特定function）、`parallel_tool_calls`を受理します。

モデルがツールを選んだ場合、非ストリーミング応答は`message.tool_calls`と`finish_reason: "tool_calls"`を返します。ストリーミング応答は`delta.tool_calls[].index/id/function.name/function.arguments`を差分で返し、最後に`finish_reason: "tool_calls"`と`[DONE]`を送ります。Lagunaの`<tool_call>...<arg_key>...`形式、JSON形式、Qwen系function/parameter形式をOpenAI形式へ変換します。

ツール付きリクエストでは、モデル固有のツール制御文字をクライアントへ途中表示しないため、ツール呼び出し部分を生成完了時に解析してからストリーム差分として送ります。通常のテキスト応答は従来どおり生成中に逐次送信します。

長い会話履歴のtokenize／prefillや、ツール呼び出し候補を内部解析している間は、10秒間隔でSSEコメント形式のheartbeatを送ります。これはOpenAI互換クライアントでは表示されませんが、ZCodeや中間プロキシが処理中の接続を無通信として切断することを防ぎます。ストリーム開始時にはOpenAI形式のassistant roleチャンクを即時送信します。生成全体の安全上限（既定15分）は引き続き有効です。

ZCodeが複数のsubagentを同時に開始した場合、MLXBarは要求を拒否せず到着順にキューへ入れます。待機中も同じheartbeatを送るため、subagent接続は生成開始まで維持されます。クライアントが接続を閉じた待機要求は自動削除されます。「設定…」→「モデル」→「並列リクエスト」で最大待機件数と最大待ち時間を変更できます。メニューバーには現在の待機件数を表示します。

### 最近のAPIログ

「設定…」→「詳細」には、公開APIへの最近のアクセスを表示します。SQLiteには最新2,000件を保持し、画面には500件まで表示します。日時、HTTP状態、処理時間、接続元がこのMacかLANか、モデル名、メッセージ数、ツール数、エラー種別を記録します。リクエスト／応答本文、ツール定義の内容、Authorizationヘッダー、APIキーは記録しません。ログは同じ画面からコピーまたは消去できます。

### APIキー

「設定…」→「APIサーバー」では、MLXBarのOpenAI互換APIに使用するキーを表示、コピー、任意の値へ変更、または再生成できます。再生成すると以前のキーは即座に無効になります。キーを要求する設定をOFFにすると、Authorizationヘッダーなしでも利用できますが、同じMac上の別プロセスからアクセス可能になるため通常はONを推奨します。

### ローカルネットワークから接続する

「設定…」→「APIサーバー」で「ローカルネットワークへ公開」を有効にします。確認後、MLXBarはすべてのIPv4 LANインターフェースで待ち受け、`http://192.168.x.x:11435`のような接続URLを表示します。別PCではそのURLをBase URLに設定し、同じ画面でコピーしたAPIキーを`Authorization: Bearer <APIキー>`として指定してください。

LAN公開中はAPIキーを無効にできません。信頼できる家庭内・社内ネットワークでのみ使用し、不要になったら公開を停止してください。接続できない場合は、両端末が同じネットワークにいること、ゲストWi-Fiやルーターの端末間通信遮断が有効でないこと、macOSのローカルネットワーク／ファイアウォール許可を確認してください。

MLXBar APIキーは`~/Library/Application Support/MLXBar/control/api-token`へ、LM Studio用キーは同じ`control`フォルダ内の秘密ファイルへ保存されます。どちらもユーザーだけが読み書きできる権限で保存され、`config.json`、診断情報、通常ログ、DMG内には含まれません。LM Studio用キーは「設定…」→「LM Studio」で設定または削除でき、モデル一覧取得と生成の両方へBearerキーとして送信されます。

## ソースからのビルド

必要なものはSwift 6、macOS SDK、Python 3.12を取得できる`uv`、`hdiutil`です。

```sh
cd Coordinator
UV_CACHE_DIR=../.uv-cache uv sync
cd ..
SWIFT_MODULE_CACHE_PATH="$PWD/.build/module-cache" \
CLANG_MODULE_CACHE_PATH="$PWD/.build/clang-cache" \
swift build --disable-sandbox -c release
./scripts/build-release.sh
```

出力は`dist/MLXBar.app`と`dist/MLXBar-1.4.1.dmg`です。`Packaging/icon.ico`からmacOS用アイコンを生成してアプリへ組み込みます。環境変数`DEVELOPER_ID_APPLICATION`を設定するとその証明書で署名し、未設定時はad-hoc署名します。Apple公証には別途Developer ID資格情報が必要です。

## テスト

```sh
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --project Coordinator --all-groups
Coordinator/.venv/bin/pytest -q Tests
swift build --disable-sandbox -c release
```

テストには、管理ソケット、公開API認証とキー交換、認証なし切替、LM Studio Bearer認証、API要求による再起動後のモデル復元、Open Interpreter別名、LibreChatモデル取得、usageストリーム、温度・Top P・繰り返しペナルティの保存値／要求値優先と各ランタイムへの伝播、長いprefillとツール解析中のheartbeat、並列subagentのFIFO順序、待機中切断・キャンセル・全件停止、キュー満杯・期限切れ、ストリーム全体時間・内部エラーのログ記録、ロード中状態、同一実パスとProviderモデルの重複排除、同名別パスの保持、ポート競合時の継続動作、設定切替、モデル走査、LM Studio互換生成、通常／ツール呼び出しストリーミング、会話履歴保持、Lagunaツール形式解析、APIログ上限、停止不能Workerの強制終了、過大入力、メモリ圧迫、異常API入力、ランタイム進捗通知・中止・再接続・旧版削除保護を含む統合・障害注入試験があります。

## 起動時のトラブルシューティング

- メニューバーに警告が出た場合は、数秒待ってからメニューを開き直してください。バックグラウンド実行の許可待ちや、過去にインストールした別ビルドの登録が残っている場合、起動シーケンスが自動的に再登録を試みてから起動します。
- バックグラウンド実行の許可を求められた場合は、「システム設定」→「一般」→「ログイン項目と機能拡張」でMLXBarを許可します。
- サービスログは`~/Library/Application Support/MLXBar/logs/coordinator.log`に保存されます。バックグラウンドサービスがクラッシュした場合の記録は、同じフォルダの`coordinator-crash.log`を確認してください。
- APIポートが他のアプリと競合しても管理サービスは停止せず、設定画面から空きポートへ変更できます。
- 上記でも解決しない場合は、「アンインストール」の手順で一度すべてのデータを削除してから再インストールしてください。

実モデルを使う受け入れ試験は、モデルの種類、サイズ、インストール済みランタイムに依存します。小型モデルを用いて、LM、VLM、LM Studioの各経路でロード→生成→アンロードを確認してください。

## アンインストール

設定の「削除」から「すべてのデータを削除して終了…」を選ぶと、バックグラウンドサービスとログイン時起動を解除したあと、設定、履歴、APIキー、MLXBarがダウンロードしたランタイム、キャッシュ、ログを削除して終了します。その後、`MLXBar.app`をゴミ箱へ移動してください。同じ操作は`mlxbarctl remove-all-data --yes`からも実行できます（ログイン項目の登録解除だけはCLIから行えず、システム設定に見た目上のエントリが残ることがありますが、サービス自体は確実に停止するため再インストールへの影響はありません）。

Hugging FaceやLM Studioなど、外部フォルダに保存されているモデル本体は読み取り専用で参照しているため、この操作では削除されません。Finderからアプリ本体だけを先に削除するとアプリ内の削除機能を利用できなくなるため、先に設定画面からデータを削除してください。

## ライセンス

Copyright (c) 2026 Yuki_Orita

MIT Licenseです。全文は[licence.md](licence.md)を参照してください。

変更履歴は[CHANGELOG.md](CHANGELOG.md)、脆弱性の連絡方法は[SECURITY.md](SECURITY.md)を参照してください。
