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
    /// `code` → (Japanese, English). Keep in sync with `Coordinator/mlxbar`
    /// (`errors.py`, `api/*.py`) and `Workers/common/server.py`.
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
