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
        )
    ]
)
