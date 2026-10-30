import XCTest
@testable import MLXBar

/// Thread-safe attempt counter for the retry tests (the loader closure runs
/// inside a `Task`, so a captured `var` would be a data race in principle).
private final class AttemptCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    @discardableResult
    func increment() -> Int {
        lock.lock(); defer { lock.unlock() }
        count += 1
        return count
    }

    var value: Int {
        lock.lock(); defer { lock.unlock() }
        return count
    }
}

/// Status decoding for the additive v3.2.0 fields, the settings-loaded gate on
/// pin operations, and the bounded settings retry (ISS-3/4/7, WP-311/411/712).
@MainActor
final class StatusDecodingTests: XCTestCase {
    // MARK: - preload (WP-311)

    /// Every state decodes, including the failure reason + code the menu bar
    /// turns into a warning line.
    func testPreloadStatusDecodesAllFields() throws {
        let value: [String: Any] = [
            "state": "failed",
            "modelId": "mlx-community/Qwen",
            "reason": "メモリが不足しています",
            "code": "MEMORY_HEADROOM_INSUFFICIENT",
            "at": 1_700_000_000.0,
        ]
        let preload = try XCTUnwrap(PreloadStatus(value))
        XCTAssertEqual(preload.state, "failed")
        XCTAssertEqual(preload.modelId, "mlx-community/Qwen")
        XCTAssertEqual(preload.reason, "メモリが不足しています")
        XCTAssertEqual(preload.code, "MEMORY_HEADROOM_INSUFFICIENT")
        XCTAssertEqual(preload.at?.timeIntervalSince1970, 1_700_000_000.0)
    }

    /// A payload without a state (or a coordinator that predates the key) is
    /// "no information", never a crash or a bogus empty state.
    func testPreloadStatusRequiresAState() {
        XCTAssertNil(PreloadStatus([:]))
        XCTAssertNil(PreloadStatus(["modelId": "m"]))
    }

    func testPreloadStatusOmitsMissingOptionalFields() throws {
        let preload = try XCTUnwrap(PreloadStatus(["state": "running"]))
        XCTAssertNil(preload.modelId)
        XCTAssertNil(preload.reason)
        XCTAssertNil(preload.code)
        XCTAssertNil(preload.at)
    }

    // MARK: - profilePinned (WP-710)

    /// Old coordinators omit `profilePinned`; `keepLoaded` was the only signal
    /// they had, so it is used as the fallback (never as the primary truth).
    func testProfilePinnedFallsBackToKeepLoaded() throws {
        let legacy = try XCTUnwrap(ResidentModel(["id": "m", "keepLoaded": true]))
        XCTAssertTrue(legacy.keepLoaded)
        XCTAssertTrue(legacy.profilePinned)

        let legacyUnpinned = try XCTUnwrap(ResidentModel(["id": "m"]))
        XCTAssertFalse(legacyUnpinned.keepLoaded)
        XCTAssertFalse(legacyUnpinned.profilePinned)
    }

    /// A session-only pin (`keepLoaded: true`, no saved profile) is explicitly
    /// *not* a pin, which is what makes the "一時" badge possible (AC-7-1).
    func testProfilePinnedIsPreferredOverKeepLoaded() throws {
        let sessionPinned = try XCTUnwrap(
            ResidentModel(["id": "m", "keepLoaded": true, "profilePinned": false]))
        XCTAssertTrue(sessionPinned.keepLoaded)
        XCTAssertFalse(sessionPinned.profilePinned)

        let profilePinned = try XCTUnwrap(
            ResidentModel(["id": "m", "keepLoaded": false, "profilePinned": true]))
        XCTAssertFalse(profilePinned.keepLoaded)
        XCTAssertTrue(profilePinned.profilePinned)
    }

    // MARK: - preload line + settings recovery (localized, no coordinator)

    /// The failure line is built from the machine code, so an English interface
    /// shows English even when the coordinator's `reason` is Japanese (the
    /// `presentError` rule, AC-3-3). The trailing period of the table sentence
    /// is folded into the template's own final period.
    func testPreloadFailureLineIsLocalizedFromTheCode() {
        let model = MenuBarViewModel()
        model.applyGUILanguage("en")
        defer { UserDefaults.standard.removeObject(forKey: MenuBarViewModel.guiLanguageDefaultsKey) }

        model.preload = PreloadStatus([
            "state": "failed",
            "modelId": "mlx-community/Qwen",
            "reason": "日本語の理由",
            "code": "MEMORY_HEADROOM_INSUFFICIENT",
        ])

        XCTAssertEqual(model.preloadStatusLine?.failed, true)
        XCTAssertEqual(
            model.preloadStatusLine?.text,
            "Could not load the resident model (reason: There is not enough memory headroom. "
                + "Unload a resident model or adjust the model's memory limit).")
    }

    func testPreloadRunningLineIsLocalized() {
        let model = MenuBarViewModel()
        model.applyGUILanguage("ja")
        defer { UserDefaults.standard.removeObject(forKey: MenuBarViewModel.guiLanguageDefaultsKey) }

        model.preload = PreloadStatus(["state": "running", "modelId": "m"])
        XCTAssertEqual(model.preloadStatusLine?.text, "常駐モデルを読み込み中…")
        XCTAssertEqual(model.preloadStatusLine?.failed, false)
    }

    /// A successful or skipped preload is the model list's story; the status
    /// line stays out of the way.
    func testPreloadLineIsHiddenWhenNothingIsWrong() {
        let model = MenuBarViewModel()
        model.preload = PreloadStatus(["state": "loaded", "modelId": "m"])
        XCTAssertNil(model.preloadStatusLine)
        model.preload = PreloadStatus(["state": "skipped"])
        XCTAssertNil(model.preloadStatusLine)
        model.preload = nil
        XCTAssertNil(model.preloadStatusLine)
    }

    /// AC-4-4: the recovery banner is announced once per backup name, is
    /// dismissible, and a *different* recovery notifies again — all without a
    /// live coordinator.
    func testSettingsRecoveryIsAnnouncedOncePerBackup() {
        let model = MenuBarViewModel()
        model.applyGUILanguage("ja")
        defer { UserDefaults.standard.removeObject(forKey: MenuBarViewModel.guiLanguageDefaultsKey) }

        model.applySettingsRecovery("config.invalid-1.json")
        XCTAssertEqual(
            model.settingsRecoveryNotice,
            "設定ファイルを読み込めなかったため、初期値に戻しました（バックアップ: config.invalid-1.json）")

        model.dismissSettingsRecoveryNotice()
        XCTAssertNil(model.settingsRecoveryNotice)

        // The coordinator keeps reporting the same recovery for the life of the
        // process; it must not re-open the banner.
        model.applySettingsRecovery("config.invalid-1.json")
        XCTAssertNil(model.settingsRecoveryNotice)

        model.applySettingsRecovery("config.invalid-2.json")
        XCTAssertNotNil(model.settingsRecoveryNotice)

        model.applyGUILanguage("en")
        model.applySettingsRecovery("config.invalid-3.json")
        XCTAssertEqual(
            model.settingsRecoveryNotice,
            "The settings file could not be read, so defaults were restored "
                + "(backup: config.invalid-3.json).")
    }

    // MARK: - settings gate + retry (WP-712 / WP-411)

    func testSettingsStartUnloadedAndBlockPinOperations() async {
        let model = MenuBarViewModel()
        XCTAssertFalse(model.settingsLoaded)

        // Must not reach the network: the guard returns first, and the reason
        // is surfaced as a transient message instead of a silent no-op.
        await model.setModelPin("mlx-community/Qwen", keepLoaded: true)
        XCTAssertNotNil(model.errorMessage)
        XCTAssertFalse(model.settingsLoaded)
    }

    /// T-4-2 / AC-4-2: the retry loop stops at the first success instead of
    /// burning the whole schedule.
    func testSettingsRetryStopsAtTheFirstSuccess() async {
        let model = MenuBarViewModel()
        let counter = AttemptCounter()

        let task = model.beginSettingsRetry(delays: Array(repeating: .milliseconds(10), count: 3)) {
            counter.increment() >= 2
        }
        // Awaiting the loop removes any dependence on timer granularity (this
        // machine coalesces 10 ms sleeps to ~200 ms under `swift test`).
        await task?.value
        XCTAssertEqual(counter.value, 2)
        XCTAssertEqual(model.settingsRetryAttempts, 2)
    }

    /// No infinite loop: exactly one attempt per scheduled delay, then stop.
    func testSettingsRetryIsBounded() async {
        let model = MenuBarViewModel()
        let counter = AttemptCounter()

        let task = model.beginSettingsRetry(delays: Array(repeating: .milliseconds(10), count: 3)) {
            counter.increment()
            return false
        }
        await task?.value
        XCTAssertEqual(counter.value, 3)
        XCTAssertEqual(model.settingsRetryAttempts, 3)
    }

    /// Only one retry loop may run at a time (the 3s/10s/30s schedule must not
    /// be started twice when `start()` races a view's `.task`).
    func testSettingsRetryRunsOnce() async {
        let model = MenuBarViewModel()
        let counter = AttemptCounter()
        let loader: () async -> Bool = {
            counter.increment()
            return false
        }

        let first = model.beginSettingsRetry(delays: [.milliseconds(10), .milliseconds(10)], loader: loader)
        let second = model.beginSettingsRetry(delays: [.milliseconds(10), .milliseconds(10)], loader: loader)

        XCTAssertNil(second, "the second call must not add a second loop")
        await first?.value
        XCTAssertEqual(counter.value, 2)
        XCTAssertEqual(model.settingsRetryAttempts, 2)
    }

    // MARK: - model warnings (TASK-002 / AC-W1..W3)

    /// The four keys the coordinator's `VISION_IMAGE_DROPPED` payload carries
    /// (`supervisor.py:600-607`) all survive decoding, so the banner can show
    /// the server's own text and the engine it applies to.
    func testModelWarningDecodesAllFields() throws {
        let warning = try XCTUnwrap(ModelWarning([
            "code": "VISION_IMAGE_DROPPED",
            "message": "このモデルは画像入力に対応していますが、mlx-lmでは画像が破棄されます",
            "messageEn": "This model supports image input, but mlx-lm drops images.",
            "engine": "mlx-lm",
        ]))
        XCTAssertEqual(warning.code, "VISION_IMAGE_DROPPED")
        XCTAssertEqual(warning.message, "このモデルは画像入力に対応していますが、mlx-lmでは画像が破棄されます")
        XCTAssertEqual(warning.messageEn, "This model supports image input, but mlx-lm drops images.")
        XCTAssertEqual(warning.engine, "mlx-lm")
    }

    /// AC-W1: a `loadedModel.warnings` array decodes to typed rows; a payload
    /// without the key (older coordinator, no warning in effect) decodes to
    /// nothing rather than to a placeholder.
    func testModelWarningListDecodesLoadedModelWarnings() {
        let loaded: [String: Any] = [
            "id": "mlx-community/Qwen3-VL-8B",
            "name": "Qwen3-VL-8B",
            "engine": "mlx-lm",
            "warnings": [[
                "code": "VISION_IMAGE_DROPPED",
                "message": "画像は破棄されます",
                "messageEn": "Images are dropped.",
                "engine": "mlx-lm",
            ]],
        ]
        let warnings = ModelWarning.list(from: loaded)
        XCTAssertEqual(warnings.count, 1)
        XCTAssertEqual(warnings.first?.code, "VISION_IMAGE_DROPPED")

        // nil `loadedModel` is the `refreshStatus` else branch; a `loadedModel`
        // without `warnings` is the "nothing wrong" case. Both show nothing.
        XCTAssertEqual(ModelWarning.list(from: nil), [])
        XCTAssertEqual(ModelWarning.list(from: ["id": "m", "name": "M"]), [])
    }

    /// AC-W3 / §5.9: the server's language-specific field wins, because it
    /// names the engine (`mlx-lm`) where the `ErrorText` table only says
    /// "this runtime".
    func testModelWarningUsesServerTextPerLanguage() throws {
        let warning = try XCTUnwrap(ModelWarning([
            "code": "VISION_IMAGE_DROPPED",
            "message": "このモデルは画像入力に対応していますが、mlx-lmでは画像が破棄されます",
            "messageEn": "This model supports image input, but mlx-lm drops images.",
            "engine": "mlx-lm",
        ]))
        XCTAssertEqual(warning.text(language: "en"),
                       "This model supports image input, but mlx-lm drops images.")
        XCTAssertEqual(warning.text(language: "ja"),
                       "このモデルは画像入力に対応していますが、mlx-lmでは画像が破棄されます")
    }

    /// AC-W3 / §5.9: without `messageEn`, English falls back to the registered
    /// `ErrorText` sentence — never to the server's Japanese text. An unknown
    /// code with no English field keeps the server message as the last resort.
    func testModelWarningFallsBackToErrorTextTable() throws {
        let noEnglish = try XCTUnwrap(ModelWarning([
            "code": "VISION_IMAGE_DROPPED",
            "message": "このモデルは画像入力に対応していますが、mlx-lmでは画像が破棄されます",
        ]))
        XCTAssertEqual(
            noEnglish.text(language: "en"),
            "This runtime does not accept images, so attached images are discarded.")
        XCTAssertFalse(noEnglish.text(language: "en").contains("画像"))

        // `message` missing as well: the table still answers in both languages.
        let bare = try XCTUnwrap(ModelWarning(["code": "VISION_IMAGE_DROPPED"]))
        XCTAssertEqual(
            bare.text(language: "ja"),
            "このランタイムでは画像入力が使われないため、添付画像は破棄されます")
        XCTAssertEqual(
            bare.text(language: "en"),
            CoordinatorErrorText.resolve(code: "VISION_IMAGE_DROPPED",
                                        serverMessage: nil, language: "en"))

        // A future code the table has never seen still shows the server text.
        let future = try XCTUnwrap(ModelWarning([
            "code": "SOME_FUTURE_WARNING",
            "message": "サーバからの新しい警告です",
        ]))
        XCTAssertEqual(future.text(language: "en"), "サーバからの新しい警告です")
        XCTAssertEqual(future.symbolName, "exclamationmark.triangle")
        XCTAssertEqual(noEnglish.symbolName, "photo.badge.exclamationmark")
    }

    /// AC-W2: a status whose `loadedModel` has no warnings produces no rows —
    /// the banner disappears on unload / re-load with another engine, because
    /// the coordinator drops the key. A string-array `warnings` (the shape of
    /// presets / hardware warnings) must not be mistaken for a model warning.
    func testModelWarningsAreClearedWhenTheStatusReportsNone() {
        XCTAssertEqual(ModelWarning.list(from: nil), [])
        XCTAssertEqual(ModelWarning.list(from: ["id": "m", "state": "loaded"]), [])
        XCTAssertEqual(ModelWarning.list(from: ["warnings": ["物理メモリ容量を検出できませんでした"]]), [])

        let model = MenuBarViewModel()
        XCTAssertTrue(model.modelWarnings.isEmpty)
        let one = ModelWarning(["code": "VISION_IMAGE_DROPPED", "message": "m"]).map { [$0] } ?? []
        model.modelWarnings = one
        XCTAssertEqual(model.modelWarnings.count, 1)
        // The `else` branch of `refreshStatus` assigns exactly this.
        model.modelWarnings = ModelWarning.list(from: nil)
        XCTAssertTrue(model.modelWarnings.isEmpty)
    }

    /// AC-W1 (A2): `ResidentModel` keeps a row's warnings, and the replica
    /// merge unions them — a replica that reports none cannot erase another
    /// replica's warning, which is how the non-primary row survives a second
    /// model being loaded.
    func testResidentModelKeepsWarningsAcrossReplicaMerge() throws {
        let warning: [String: Any] = [
            "code": "VISION_IMAGE_DROPPED",
            "message": "画像は破棄されます",
            "messageEn": "Images are dropped.",
            "engine": "mlx-lm",
        ]
        let resident = try XCTUnwrap(ResidentModel(["id": "m", "warnings": [warning]]))
        XCTAssertEqual(resident.warnings.count, 1)
        XCTAssertEqual(resident.warnings.first?.code, "VISION_IMAGE_DROPPED")

        // No warnings key: an ordinary row.
        XCTAssertTrue(try XCTUnwrap(ResidentModel(["id": "m"])).warnings.isEmpty)

        // Replica with the warning + replica without it, in either order.
        let mergedForward = ModelWarning.merge([warning], [])
        XCTAssertEqual(mergedForward.count, 1)
        XCTAssertEqual(ModelWarning.merge([], [warning]).count, 1)
        // The same warning on both replicas is one row, not two.
        XCTAssertEqual(ModelWarning.merge([warning], [warning]).count, 1)
        XCTAssertTrue(ModelWarning.merge([], []).isEmpty)

        let mergedResident = try XCTUnwrap(
            ResidentModel(["id": "m", "warnings": ModelWarning.merge([warning], [])]))
        XCTAssertEqual(mergedResident.warnings.map(\.code), ["VISION_IMAGE_DROPPED"])
    }
}
