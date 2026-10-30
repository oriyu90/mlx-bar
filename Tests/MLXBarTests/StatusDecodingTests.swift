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
}
