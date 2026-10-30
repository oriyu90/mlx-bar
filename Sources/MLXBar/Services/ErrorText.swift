import Foundation

/// Turns a coordinator error (`code` + optional server `message`) into text for
/// the chosen interface language.
///
/// The coordinator and its workers always send a stable machine `code` and,
/// since v1.7.1, a Japanese `message`. External API consumers still get both
/// fields unchanged; this type is only the GUI's presentation layer. Resolving
/// by `code` — rather than trusting the server string — keeps the displayed text
/// in the user's language even when the underlying failure came from an upstream
/// runtime (mlx-lm / mlx-vlm / transformers) that only speaks English, and it
/// still degrades gracefully: an unknown `code` falls back to the server
/// `message`, then to the `code` itself.
///
/// When a new `MLXBarError` code is added on the Python side, add it here too
/// (see `mlx-bar.md`).
enum CoordinatorErrorText {
    /// `code` → (Japanese, English). Keep in sync with `Coordinator/mlxbar/**`
    /// (all of it: `errors.py`, `api/`, `workers/`, `rag/`, `runtimes/`, …) and
    /// `Workers/**` (`common/server.py` and the per-engine adapters), which is
    /// exactly what `Tests/test_v3_2_0_error_contract.py` scans.
    static let table: [String: (ja: String, en: String)] = [
        "AUTHENTICATION_FAILED": (
            "APIキーが正しくありません。接続先の設定を確認してください",
            "The API key is not valid. Check the connection settings."),
        "MODEL_NOT_FOUND": (
            "指定されたモデルが見つかりません。モデル一覧を再スキャンしてください",
            "That model was not found. Try rescanning the model list."),
        "MODEL_NOT_LOADED": (
            "モデルがロードされていません。MLXBarでモデルをロードしてください",
            "No model is loaded. Load a model in MLXBar."),
        "MODEL_INCOMPATIBLE": (
            "このモデルは読み込めませんでした",
            "This model could not be loaded."),
        "MODEL_REQUIRES_REMOTE_CODE": (
            "このモデルはリモートコードの実行が必要なため読み込めません",
            "This model needs remote code execution and cannot be loaded."),
        "ENGINE_BUSY": (
            "別の生成が実行中のため、この操作は今はできません。完了を待つか強制実行を指定してください",
            "Another generation is running, so this cannot be done right now. Wait for it to finish or force it."),
        "QUEUE_FULL": (
            "生成待ちが上限に達しています。しばらくしてからもう一度お試しください",
            "The generation queue is full. Try again shortly."),
        "QUEUE_TIMEOUT": (
            "生成待ち時間が上限を超えました",
            "The request waited in the queue longer than the limit allows."),
        "INVALID_ENGINE": (
            "エンジンは mlx-lm または mlx-vlm を指定してください",
            "The engine must be mlx-lm or mlx-vlm."),
        "INVALID_REQUEST": (
            "リクエストの内容が正しくありません",
            "The request is not valid."),
        "INVALID_SETTINGS": (
            "設定の値が正しくありません",
            "One or more settings values are not valid."),
        "INVALID_API_TOKEN": (
            "APIキーの値が正しくありません",
            "The API key value is not valid."),
        "INVALID_SLOT": (
            "指定されたランタイムスロットが正しくありません",
            "That runtime slot is not valid."),
        "UNSUPPORTED_PARAMETER": (
            "対応していないパラメーターが指定されました",
            "An unsupported parameter was supplied."),
        "UNSUPPORTED_ENDPOINT": (
            "対応していないエンドポイントです。/v1/chat/completions を使用してください",
            "Unsupported endpoint. Use /v1/chat/completions."),
        "INPUT_TOO_LARGE": (
            "入力が大きすぎます",
            "The input is too large."),
        "MEMORY_BUDGET_EXCEEDED": (
            "空きメモリが不足しているため、この操作を安全に実行できません",
            "There is not enough free memory to do this safely."),
        "MEMORY_PRESSURE": (
            "macOSがメモリ逼迫を報告しているため、新しいモデルはロードしません",
            "macOS reports memory pressure, so no new model will be loaded."),
        "MODEL_MEMORY_LIMIT": (
            "モデルのメモリ使用量が上限を超えています",
            "The model's memory use exceeds the configured limit."),
        "RUNTIME_MEMORY_LIMIT_UNAVAILABLE": (
            "このランタイムはモデル単位のメモリ上限を確認できないため、複数常駐では使用できません",
            "This runtime cannot confirm a per-model memory limit, so it cannot be used in the multi-model pool."),
        "RUNTIME_NOT_INSTALLED": (
            "必要なランタイムがインストールされていません",
            "The required runtime is not installed."),
        "RUNTIME_DELETE_FAILED": (
            "ランタイムを削除できませんでした",
            "The runtime could not be removed."),
        "ROLLBACK_UNAVAILABLE": (
            "切り戻せるランタイムがありません",
            "There is no runtime to roll back to."),
        "UPDATE_PROBE_FAILED": (
            "ランタイムの検証に失敗しました",
            "The runtime failed verification."),
        "LISTENER_SWITCH_FAILED": (
            "APIサーバーのポートまたはアドレスを切り替えられませんでした",
            "The API server port or address could not be switched."),
        "JOB_NOT_FOUND": (
            "指定されたジョブが見つかりません",
            "That job was not found."),
        "TOOL_PARSE_FAILED": (
            "モデルのtool callを解析できませんでした",
            "The model's tool call could not be parsed."),
        "GENERATION_FAILED": (
            "生成に失敗しました",
            "Generation failed."),
        "PROTOCOL_MISMATCH": (
            "内部コンポーネントのバージョンが一致しません。MLXBarを再起動してください",
            "Internal components are out of sync. Restart MLXBar."),
        "INTERNAL_ERROR": (
            "内部エラーが発生しました。しばらくしてからもう一度お試しください",
            "An internal error occurred. Try again shortly."),
        "CLIENT_DISCONNECTED": (
            "接続が切断されました",
            "The connection was dropped."),
        // v2.8.0: per-model idle countdown extension (`model extend-idle` /
        // the menu-bar extend field). Added here in v2.8.1 because they surface
        // through the GUI too.
        "INVALID_IDLE_EXTENSION": (
            "延長する秒数は1〜86,400秒の間で指定してください",
            "The extension must be between 1 and 86,400 seconds."),
        "MODEL_NOT_IDLE_MANAGED": (
            "このモデルには解放までの時間がありません。常駐固定かkeep Loadedのモデルです",
            "This model has no idle countdown: it is pinned or set to keep loaded."),
        // v2.8.1: CLI job-wait cap.
        "JOB_WAIT_TIMEOUT": (
            "ジョブが30分以内に終了しませんでした",
            "The job did not finish within 30 minutes."),
        // v2.10.0: oMLX runtime (setup + routing + model load/unload).
        "OMLX_NOT_AVAILABLE": (
            "oMLXプロバイダーが初期化されていません。MLXBarを再起動してください",
            "The oMLX provider is not initialized. Restart MLXBar."),
        "OMLX_CONNECTION_FAILED": (
            "oMLXサーバーに接続できませんでした。サーバーの起動とURL設定を確認してください",
            "Could not reach the oMLX server. Check that it is running and the URL is right."),
        "OMLX_LIST_FAILED": (
            "oMLXのモデル一覧を取得できませんでした",
            "Could not list the oMLX models."),
        "OMLX_LOAD_FAILED": (
            "oMLXでのモデルロードに失敗しました",
            "Loading the model on oMLX failed."),
        "OMLX_UNLOAD_FAILED": (
            "oMLXでのモデルアンロードに失敗しました",
            "Unloading the model from oMLX failed."),
        "OMLX_LOAD_TIMEOUT": (
            "oMLXでのモデルロードが時間内に完了しませんでした",
            "Loading the model on oMLX timed out."),
        "OMLX_UNLOAD_TIMEOUT": (
            "oMLXでのモデルアンロードが時間内に完了しませんでした",
            "Unloading the model from oMLX timed out."),
        "OMLX_TIMEOUT": (
            "oMLXへの要求が時間内に完了しませんでした",
            "The request to oMLX timed out."),
        "OMLX_REQUEST_FAILED": (
            "oMLXへの要求が失敗しました",
            "The request to oMLX failed."),
        "OMLX_ERROR": (
            "oMLXがエラーを返しました",
            "oMLX returned an error."),
        "OMLX_BREW_NOT_FOUND": (
            "Homebrewが見つかりません。https://github.com/jundot/omlx/releases から手動でインストールできます",
            "Homebrew was not found. Install oMLX manually from https://github.com/jundot/omlx/releases."),
        "OMLX_INSTALL_FAILED": (
            "Homebrewの処理は完了しましたが、omlxコマンドが見つかりません。ターミナルで omlx --version を確認してください",
            "Homebrew finished, but the omlx command was not found. Run omlx --version in Terminal to check."),
        "OMLX_NOT_INSTALLED": (
            "omlxコマンドが見つかりません。先にoMLXをインストールしてください",
            "The omlx command was not found. Install oMLX first."),
        "OMLX_COMMAND_TIMEOUT": (
            "oMLXのコマンドが時間内に完了しなかったため中止しました",
            "The oMLX command did not finish within its time limit and was stopped."),
        "OMLX_COMMAND_FAILED": (
            "oMLXのコマンドが失敗しました",
            "The oMLX command failed."),
        // v3.2.0: memory admission split (ISS-6), bundled-loader / format /
        // tokenizer classification (ISS-5), and the vision-image warning
        // (ISS-2). These codes are part of the E1/E2 interface contract, so they
        // are registered here even though some are currently only emitted by
        // the coordinator: an unknown code would otherwise fall through to the
        // server's Japanese message in an English interface (AC-5-6).
        "MEMORY_HEADROOM_INSUFFICIENT": (
            "メモリの余白が不足しています。常駐モデルを減らすか、モデルのメモリ上限を調整してください",
            "There is not enough memory headroom. Unload a resident model or adjust the model's memory limit."),
        "MEMORY_BUDGET_TOTAL": (
            "モデル用に確保した総メモリ予算を超えています。常駐設定を見直してください",
            "The total memory budget for models is exceeded. Review the residency settings."),
        "MEMORY_POOL_EXHAUSTED": (
            "モデルを置ける空きスロットがありません。常駐モデルを減らしてください",
            "No resident model slot is free. Unload a resident model."),
        "MEMORY_POSTLOAD_EXCEEDED": (
            "ロード後の実測メモリが上限を超えたため、モデルを解放しました",
            "Measured memory after loading exceeded the limit, so the model was unloaded."),
        "MODEL_NEEDS_BUNDLED_LOADER": (
            "このモデルは同梱ローダが必要です。対応版で読み込めます",
            "This model requires a bundled loader, available in a later version."),
        "MODEL_FORMAT_UNSUPPORTED": (
            "このモデルの形式には対応していません",
            "This model's format is not supported."),
        "MODEL_QUANT_UNSUPPORTED": (
            "このモデルの量子化形式には対応していません",
            "This model's quantization format is not supported."),
        "MODEL_TOKENIZER_MISSING": (
            "このモデルにはトークナイザーが含まれていません",
            "This model does not include a tokenizer."),
        "VISION_IMAGE_DROPPED": (
            "このランタイムでは画像入力が使われないため、添付画像は破棄されます",
            "This runtime does not accept images, so attached images are discarded."),
        // v3.2.0 (WP-R1): request-validation codes the API already returned but
        // that were never registered here, so an English interface showed the
        // server's Japanese `message` verbatim (AC-5-6). `INVALID_PROFILE` is
        // new in v3.2.0 (per-model residency-profile merge); the rest predate
        // it (v2.6.0-v2.10.0). `Tests/test_v3_2_0_error_contract.py` derives the
        // expected set from the Python sources themselves (`Coordinator/mlxbar/**`
        // and `Workers/**` since v3.2.3) instead of a hand-kept array, so the
        // next added code fails that test until it is registered here.
        "INVALID_PROFILE": (
            "常駐プロフィールの値が正しくありません。モデルID・keepLoaded・replicas（1〜8）・maxMemoryGB（1〜512）を確認してください",
            "The residency profile values are not valid. Check the model ID, keepLoaded, replicas (1-8), and maxMemoryGB (1-512)."),
        "INVALID_PIN": (
            "pinはtrue/falseで指定してください",
            "pin must be true or false."),
        "INVALID_PRESET_MODE": (
            "modeはlight/fullで指定してください",
            "mode must be light or full."),
        "INVALID_SETUP": (
            "初期設定の値が正しくありません。モデルフォルダ・常駐数（1〜8）・プリセットを確認してください",
            "The setup values are not valid. Check the models folder, the resident-model count (1-8), and the preset."),
        "INVALID_IMPORT": (
            "インポートする設定データが正しくありません",
            "The settings data being imported is not valid."),
        "MODEL_NOT_ROUTED": (
            "このモデルはoMLXにルーティングされていません",
            "This model is not routed to oMLX."),
        "RESPONSE_FORMAT_INVALID": (
            "生成結果がresponse_formatに一致しませんでした",
            "The generated output did not match response_format."),
        // v3.2.2 (TASK-003): index が参照する重みファイルの実在欠落。
        // 旧 MODEL_FORMAT_UNSUPPORTED の「形式非対応」表示では誤診になるため分離。
        "MODEL_WEIGHTS_INCOMPLETE": (
            "モデルの重みファイルが不足しています。ダウンロードが不完全か、配置が変わっている可能性があります",
            "Model weight files are missing. The download may be incomplete or files may have moved."),
        // v3.2.3 (WP-003 / F-09): the codes the coordinator, its worker stack,
        // the RAG store, the runtime updater and the LM Studio bridge emit that
        // this table did not carry. The sync test used to scan `api/*.py` only,
        // so these fell through to the server's Japanese `message` in an
        // English interface with a green suite. `UNKNOWN_METHOD` is
        // deliberately absent: it is a JSON-RPC dispatch detail with no
        // user-visible path, so the contract test lists it in `INTERNAL_CODES`.
        "COUNT_TOKENS_UNAVAILABLE": (
            "このランタイムはトークン数の計測に対応していません",
            "This runtime cannot count tokens."),
        "ENGINE_MISMATCH": (
            "このモデルは選択したエンジンでは実行できません。oMLXなど別のエンジンに割り当てられています",
            "This model cannot run on the selected engine. It is assigned to another engine, such as oMLX."),
        "GENERATION_TIMEOUT": (
            "生成が安全上限の時間を超えたため停止しました",
            "Generation was stopped because it exceeded the safety time limit."),
        "INSUFFICIENT_STORAGE": (
            "ランタイムの更新には1GB以上の空き容量が必要です",
            "Updating the runtime needs at least 1 GB of free disk space."),
        "INVALID_GIT_REF": (
            "git-refはcommit SHAで指定してください",
            "The git ref must be a commit SHA."),
        "INVALID_HOST": (
            "待ち受けアドレスは 127.0.0.1 または 0.0.0.0 を指定してください",
            "The listen address must be 127.0.0.1 or 0.0.0.0."),
        "INVALID_PORT": (
            "ポートは1024〜65535の間で指定してください",
            "The port must be between 1024 and 65535."),
        "INVALID_VERSION": (
            "バージョンの形式が正しくありません",
            "The version string is not in a valid format."),
        "JOB_CANCELLED": (
            "処理を中止しました",
            "The job was cancelled."),
        "JOB_INTERRUPTED": (
            "サービス再起動により処理が中断されました",
            "The job was interrupted by a service restart."),
        "LMSTUDIO_LOAD_FAILED": (
            "LM Studioでモデルをロードできませんでした",
            "LM Studio could not load the model."),
        "LMSTUDIO_REQUEST_FAILED": (
            "LM Studioへの生成リクエストが失敗しました",
            "The generation request to LM Studio failed."),
        "LMSTUDIO_UNAVAILABLE": (
            "LM Studioに接続できません。起動状態とURL・APIキーの設定を確認してください",
            "Could not reach LM Studio. Check that it is running and that the URL and API key are right."),
        "LMSTUDIO_UNLOAD_FAILED": (
            "LM Studioのモデルを解放できませんでした",
            "The model could not be unloaded from LM Studio."),
        "MODEL_LOAD_TIMEOUT": (
            "モデルのロードが制限時間内に完了しませんでした",
            "Loading the model did not finish within the time limit."),
        "PORT_IN_USE": (
            "指定したポートは既に使用されています。別のポートを指定してください",
            "That port is already in use. Choose a different port."),
        "RAG_COLLECTION_EXISTS": (
            "同じ名前のコレクションが既に存在します",
            "A collection with that name already exists."),
        "RAG_COLLECTION_FULL": (
            "コレクションのチャンク上限に達しました。不要なドキュメントを削除するか、上限を引き上げてください",
            "The collection reached its chunk limit. Delete documents you no longer need or raise the limit."),
        "RAG_COLLECTION_NOT_FOUND": (
            "指定したコレクションが見つかりません",
            "That collection was not found."),
        "RAG_DISABLED": (
            "ナレッジベース機能が無効です。設定 > 知識ベース で有効にしてください",
            "The knowledge base is disabled. Enable it in Settings > Knowledge Base."),
        "RAG_DOCUMENT_NOT_FOUND": (
            "指定したドキュメントが見つかりません",
            "That document was not found."),
        "RAG_DOCUMENT_TOO_LARGE": (
            "ドキュメントが大きすぎます。分割して取り込んでください",
            "The document is too large. Split it and ingest the parts."),
        "RAG_EMBEDDING_DIM_MISMATCH": (
            "埋め込みベクトルの次元がコレクションと一致しません。埋め込みモデルを変更した場合は、コレクションを作り直してください",
            "The embedding dimension does not match the collection. If you changed the embedding model, recreate the collection."),
        "RAG_EMBEDDING_NOT_CONFIGURED": (
            "埋め込みエンドポイントが設定されていません。URLとモデル名を設定してください",
            "The embedding endpoint is not configured. Set its URL and model name."),
        "RAG_EMBEDDING_UNAVAILABLE": (
            "埋め込みエンドポイントを利用できません。起動状態とURLを確認してください",
            "The embedding endpoint is unavailable. Check that it is running and the URL is right."),
        "RAG_EMPTY_DOCUMENT": (
            "取り込む本文がありません。テキストまたはファイルを指定してください",
            "There is no text to ingest. Provide text or a file."),
        "RAG_EMPTY_QUERY": (
            "検索クエリが空です",
            "The search query is empty."),
        "RAG_FILE_NOT_FOUND": (
            "取り込むファイルが見つかりません",
            "The file to ingest was not found."),
        "RAG_INVALID_NAME": (
            "コレクション名は英数字で始まり、英数字・ドット・ハイフン・アンダースコアのみ、64文字以内で指定してください",
            "A collection name must start with a letter or digit and contain only letters, digits, dots, hyphens, and underscores, up to 64 characters."),
        "ROLLBACK_FAILED": (
            "更新に失敗し、元のランタイムへの復元にも失敗しました",
            "The update failed and rolling back to the previous runtime also failed."),
        "UPDATE_CHECK_FAILED": (
            "最新版を確認できませんでした。ネットワーク接続を確認してください",
            "Could not check for the latest version. Check your network connection."),
        "UPDATE_FAILED": (
            "ランタイムの更新に失敗しました",
            "The runtime update failed."),
        "UPDATE_TIMEOUT": (
            "更新処理が制限時間内に完了しなかったため中止しました",
            "The update did not finish within the time limit and was stopped."),
        "WORKER_CRASHED": (
            "モデルWorkerが停止しました。もう一度お試しください",
            "The model worker stopped. Try again."),
        "WORKER_STALLED": (
            "モデルWorkerが応答しないため再起動しました",
            "The model worker stopped responding and was restarted."),
        // v3.2.3 (WP-010 / WP-004): new codes. `MODEL_CRASH_LOOP` is refused by
        // the supervisor and the pool; `MODEL_LOAD_DEGRADED` is the warning the
        // supervisor attaches when the catalog asked for one engine (or path)
        // and another one served the model.
        "MODEL_CRASH_LOOP": (
            "このモデルは300秒以内に繰り返しクラッシュしたため、自動での再ロードを停止しました。モデルをアンロードするか、ピン留めを変更してから再試行してください",
            "Automatic reload was stopped because this model crashed repeatedly within 300 seconds. Unload the model or change its pin, then try again."),
        "MODEL_LOAD_DEGRADED": (
            "カタログと異なる経路で読み込みました。対応機能や品質が変わる可能性があります",
            "Loaded through a different path than the catalog entry. Capabilities or quality may differ."),
    ]

    /// Best display string for the given error, in `language` ("ja" or "en").
    ///
    /// Order: localized text for a known `code`; then the server `message` (only
    /// when it is not the bare `code` echoed back); then the `code`; then a
    /// generic sentence.
    static func resolve(code: String?, serverMessage: String?, language: String) -> String {
        let japanese = language != "en"
        let trimmedMessage = serverMessage?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let code, let entry = table[code] {
            return japanese ? entry.ja : entry.en
        }
        if let message = trimmedMessage, !message.isEmpty, message != code {
            return message
        }
        if let code, !code.isEmpty {
            return code
        }
        return japanese ? "エラーが発生しました" : "Something went wrong."
    }
}
