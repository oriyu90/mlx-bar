import SwiftUI

@main
struct MLXBarApp: App {
    @StateObject private var model: MenuBarViewModel

    init() {
        let model = MenuBarViewModel()
        _model = StateObject(wrappedValue: model)
        Task { @MainActor in await model.start() }
    }

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            Label(model.shortStatus, systemImage: model.icon)
        }
        .menuBarExtraStyle(.window)

        Window("モデル", id: "models") {
            ModelCatalogView(model: model)
                .frame(minWidth: 720, minHeight: 460)
        }
        Window("クイックチャット", id: "chat") {
            QuickChatView(model: model)
                .frame(minWidth: 640, minHeight: 520)
        }
        Settings {
            MLXBarSettingsView(model: model)
                .frame(width: 640, height: 480)
        }
        Window("ランタイム管理", id: "runtimes") {
            RuntimeManagerView(model: model)
                .frame(minWidth: 700, minHeight: 460)
        }
    }
}
