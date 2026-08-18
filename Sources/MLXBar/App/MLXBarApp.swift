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
                .environment(\.locale, Locale(identifier: model.guiLanguage))
        } label: {
            Label(model.shortStatus, systemImage: model.icon)
        }
        .menuBarExtraStyle(.window)

        // Window titles are read once when the scene is created, so they use
        // the English name rather than tracking the language picker.
        Window("Models", id: "models") {
            ModelCatalogView(model: model)
                .frame(minWidth: 720, minHeight: 460)
                .environment(\.locale, Locale(identifier: model.guiLanguage))
        }
        Window("Quick Chat", id: "chat") {
            QuickChatView(model: model)
                .frame(minWidth: 640, minHeight: 520)
                .environment(\.locale, Locale(identifier: model.guiLanguage))
        }
        Settings {
            MLXBarSettingsView(model: model)
                .frame(minWidth: 820, idealWidth: 920, minHeight: 620, idealHeight: 720)
                .environment(\.locale, Locale(identifier: model.guiLanguage))
        }
    }
}
