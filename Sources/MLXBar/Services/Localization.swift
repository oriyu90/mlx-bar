import Foundation

/// Resolves interface strings for the language chosen in Settings.
///
/// The translations ship inside the SwiftPM resource bundle, which SwiftUI's
/// implicit `Text("key")` lookup never searches — it only consults
/// `Bundle.main`. Selecting the bundle explicitly also decouples the interface
/// language from the system locale, so the Settings picker actually decides
/// which strings are shown.
@MainActor
enum AppLanguage {
    static let supported = ["en", "ja"]
    /// Japanese literals in the source double as localization keys, so the
    /// Japanese interface needs no table of its own.
    static let sourceLanguage = "ja"

    static var current = "en"
    private static var bundles: [String: Bundle] = [:]

    static func bundle(for code: String) -> Bundle? {
        if let cached = bundles[code] { return cached }
        guard let path = Bundle.module.path(forResource: code, ofType: "lproj"),
              let bundle = Bundle(path: path) else { return nil }
        bundles[code] = bundle
        return bundle
    }

    static func text(_ key: String, language: String? = nil) -> String {
        let code = language ?? current
        guard code != sourceLanguage, let bundle = bundle(for: code) else { return key }
        return bundle.localizedString(forKey: key, value: key, table: nil)
    }
}

/// Shorthand for `AppLanguage.text` used throughout the views.
@MainActor
func LS(_ key: String) -> String { AppLanguage.text(key) }
