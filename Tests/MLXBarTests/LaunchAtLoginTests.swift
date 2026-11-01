import Foundation
import XCTest

@testable import MLXBar

/// v4.0.2: `SMAppService.mainApp` registers the running executable, so a login
/// item created from a debug build points at a bare Mach-O. macOS can only open
/// such a file with Terminal, which is how a login produced a terminal window
/// running `…/.build/arm64-apple-macosx/debug/MLXBar ; exit;`. Only a real
/// bundle may register; these cases pin that rule.
@MainActor
final class LaunchAtLoginTests: XCTestCase {
    func testAnInstalledAppBundleIsRecognised() {
        XCTAssertTrue(MenuBarViewModel.isAppBundle(
            URL(fileURLWithPath: "/Applications/MLXBar.app")))
    }

    func testADebugExecutableIsNotABundle() {
        XCTAssertFalse(MenuBarViewModel.isAppBundle(
            URL(fileURLWithPath: "/Users/user/repo/.build/arm64-apple-macosx/debug/MLXBar")))
    }

    func testTheBundlePathWithoutTheExtensionIsNotABundle() {
        XCTAssertFalse(MenuBarViewModel.isAppBundle(
            URL(fileURLWithPath: "/Applications/MLXBar")))
    }

    func testTheCheckIgnoresCase() {
        XCTAssertTrue(MenuBarViewModel.isAppBundle(
            URL(fileURLWithPath: "/Applications/MLXBar.APP")))
    }
}
