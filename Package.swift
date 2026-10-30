// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "MLXBar",
    defaultLocalization: "en",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "MLXBar", targets: ["MLXBar"])],
    targets: [
        .executableTarget(
            name: "MLXBar",
            path: "Sources/MLXBar",
            resources: [.process("Resources")],
            swiftSettings: [.swiftLanguageMode(.v6)]
        ),
        // v3.2.0 (WP-910): there was no test target at all, so every GUI
        // guarantee — error lifetime, language mirror, pin decoding — was
        // unverified. The test target keeps the package's Swift 5 language mode
        // because XCTest's nonisolated `XCTestCase` requirements and the
        // app's `@MainActor` view model do not mix under Swift 6's strict
        // actor-isolation checking.
        .testTarget(
            name: "MLXBarTests",
            dependencies: ["MLXBar"],
            path: "Tests/MLXBarTests",
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
