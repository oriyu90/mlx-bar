import XCTest
@testable import MLXBar

/// ISS-1 / WP-110: the transient error banner must live for exactly one
/// lifetime and must never be cleared by the timer of an *older* error.
///
/// Durations are injected (`errorDismissDuration`) so nothing here waits 20
/// seconds; the waits are deliberately several times the configured lifetime so
/// a loaded machine cannot make the assertions race.
@MainActor
final class ErrorLifecycleTests: XCTestCase {
    /// T-1-1 / AC-1-1: setting a message clears it after its own lifetime.
    ///
    /// The wait is 20x the lifetime: `swift test` coalesces short sleeps to a
    /// few hundred milliseconds, and the assertion is about "eventually", not
    /// about the exact wake-up.
    func testErrorMessageClearsAfterItsLifetime() async {
        let model = MenuBarViewModel()
        model.errorDismissDuration = .milliseconds(50)

        model.errorMessage = "first"
        XCTAssertEqual(model.errorMessage, "first")

        try? await Task.sleep(for: .seconds(1))
        XCTAssertNil(model.errorMessage, "the transient banner must clear itself")
    }

    /// T-1-2 / AC-1-2: an error raised while an earlier one is still showing
    /// keeps its own full lifetime.
    ///
    /// The first lifetime is deliberately long: with the pre-fix
    /// `try? await Task.sleep(...)`, cancelling that timer resumed it
    /// immediately and its trailing `errorMessage = nil` wiped the second
    /// message, so "is the second message still there half a second later?" is
    /// the discriminating question. Its own expiry is checked separately with a
    /// short lifetime, so neither half depends on tight timing.
    func testLaterErrorSurvivesTheCancelledTimer() async {
        let model = MenuBarViewModel()
        model.errorDismissDuration = .seconds(3)

        model.errorMessage = "first"
        // Replace it well inside the first message's window, so the first
        // timer is cancelled rather than allowed to expire.
        try? await Task.sleep(for: .milliseconds(100))
        model.errorMessage = "second"

        try? await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(model.errorMessage, "second",
                       "a newer error must not be erased by the older timer")

        // And the newer message still expires on its own lifetime.
        model.errorDismissDuration = .milliseconds(50)
        model.errorMessage = "third"
        try? await Task.sleep(for: .seconds(1))
        XCTAssertNil(model.errorMessage)
    }

    /// A `nil` assignment (dismissal or a fresh `perform {}`) cancels the timer
    /// and does not resurrect anything afterwards.
    func testClearingDoesNotResurrectTheMessage() async {
        let model = MenuBarViewModel()
        model.errorDismissDuration = .milliseconds(50)

        model.errorMessage = "transient"
        model.errorMessage = nil
        try? await Task.sleep(for: .seconds(1))
        XCTAssertNil(model.errorMessage)
    }

    /// AC-1-4 guard: the connection state is its own channel, so an error
    /// message and "the coordinator is unreachable" cannot overwrite each
    /// other. Both start false/empty and the connection flag is read-only from
    /// outside the view model.
    func testConnectionStateIsIndependentOfTheErrorMessage() {
        let model = MenuBarViewModel()
        XCTAssertFalse(model.connectionLost)
        XCTAssertNil(model.errorMessage)
        XCTAssertEqual(model.icon, "moon.zzz")
    }
}
