import XCTest
@testable import MLXBar

/// ISS-4 / WP-410: the interface language is mirrored into `UserDefaults` so the
/// very first frame of the next launch is already in the right language, while
/// the coordinator stays authoritative whenever it answers.
@MainActor
final class LanguageMirrorTests: XCTestCase {
    private var key: String { MenuBarViewModel.guiLanguageDefaultsKey }

    private func clearMirror() {
        UserDefaults.standard.removeObject(forKey: key)
    }

    /// T-4-1 / AC-4-1: a stored "ja" is used at init, before any network call.
    func testInitialLanguageComesFromTheStoredMirror() {
        UserDefaults.standard.set("ja", forKey: key)
        defer { clearMirror() }

        let model = MenuBarViewModel()

        XCTAssertEqual(model.guiLanguage, "ja")
        XCTAssertEqual(AppLanguage.current, "ja",
                       "LS(_:) resolves against AppLanguage.current, so it must move with guiLanguage")
    }

    /// T-4-3 / AC-4-3: nothing here talks to the coordinator (no `start()`), so
    /// the mirrored language is what the UI keeps when the service is absent.
    func testMirroredLanguageSurvivesWithoutTheService() {
        UserDefaults.standard.set("ja", forKey: key)
        defer { clearMirror() }

        let model = MenuBarViewModel()
        XCTAssertFalse(model.serviceRunning)
        XCTAssertEqual(model.guiLanguage, "ja")
        XCTAssertEqual(AppLanguage.current, "ja")
    }

    /// T-4-5 / AC-4-5: the server value wins over the mirror, and the mirror is
    /// rewritten to match it (so the next launch agrees).
    func testAppliedServerLanguageOverridesTheMirror() {
        UserDefaults.standard.set("ja", forKey: key)
        defer { clearMirror() }

        let model = MenuBarViewModel()
        XCTAssertEqual(model.guiLanguage, "ja")

        model.applyGUILanguage("en")

        XCTAssertEqual(model.guiLanguage, "en")
        XCTAssertEqual(AppLanguage.current, "en")
        XCTAssertEqual(UserDefaults.standard.string(forKey: key), "en")
    }

    /// An unsupported code is ignored rather than stored: the picker can only
    /// produce "en"/"ja", and a stale value must not blank the interface.
    func testUnsupportedLanguageIsIgnored() {
        UserDefaults.standard.set("ja", forKey: key)
        defer { clearMirror() }

        let model = MenuBarViewModel()
        model.applyGUILanguage("fr")

        XCTAssertEqual(model.guiLanguage, "ja")
        XCTAssertEqual(UserDefaults.standard.string(forKey: key), "ja")
    }
}
