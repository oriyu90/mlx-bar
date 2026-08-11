import SwiftUI

struct MenuBarView: View {
    @ObservedObject var model: MenuBarViewModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings
    @Environment(\.dismiss) private var dismiss
    @State private var isSelectingFolder = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: model.icon).font(.title2)
                VStack(alignment: .leading) {
                    Text(model.loadingModelName.map { model.guiLanguage == "ja" ? "ロード中 · \(model.loadingEngine ?? "") · \($0)" : "Loading · \(model.loadingEngine ?? "") · \($0)" }
                         ?? model.loadedName.map { "Loaded · \(model.loadedEngine ?? "") · \($0)" }
                         ?? model.serviceStatus)
                        .font(.headline).lineLimit(1)
                    if let phase = model.loadingPhase { Text(phase).font(.caption).foregroundStyle(.secondary) }
                    if model.loadedName != nil {
                        Text("Max token \(MenuBarViewModel.tokenCount(model.effectiveMaxTokens))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text(model.apiURL).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if model.loadingModelName == nil, model.loadedName != nil {
                    Button { model.copyLoadedModelName() } label: {
                        Image(systemName: "doc.on.doc").accessibilityLabel("モデル名をコピー")
                    }.buttonStyle(.plain)
                }
                if model.busy { ProgressView().controlSize(.small) }
            }

            if let status = model.modelCopyStatus {
                Text(status).font(.caption).foregroundStyle(.secondary)
            }

            if model.loadedName != nil || model.loadingModelName != nil {
                Label(model.modelActivityText,
                      systemImage: model.activeRequestCount > 0 || model.queuedRequestCount > 0 ? "waveform.circle.fill" :
                        model.loadingModelName != nil ? "arrow.triangle.2.circlepath" : "checkmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(model.activeRequestCount > 0 || model.queuedRequestCount > 0 ? .orange :
                                     model.loadingModelName != nil ? .secondary : .green)
            }

            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red).lineLimit(3)
            }
            Divider()
            Button("モデルを選択…") { openWindow(id: "models"); dismiss() }.keyboardShortcut("m")
            if model.loadedName == nil {
                Button("クイックチャット…") { openWindow(id: "chat"); dismiss() }.disabled(true)
            } else {
                Button("クイックチャット…") { openWindow(id: "chat"); dismiss() }.keyboardShortcut("n")
                Button("アンロード") { Task { await model.unload() } }
            }
            if model.currentRequestID != nil {
                Button(model.cancellationInProgress ? "生成を停止処理中…" : "生成をキャンセル") {
                    Task { await model.cancelGeneration() }
                }.disabled(model.cancellationInProgress)
            } else if model.activeRequestCount > 0 || model.queuedRequestCount > 0 {
                Button(model.cancellationInProgress ? "すべて停止処理中…" : "すべての生成を停止") {
                    Task { await model.cancelAllGenerations() }
                }.disabled(model.cancellationInProgress)
            }
            Divider()
            HStack {
                Button("今すぐ再スキャン") { Task { await model.scan() } }
                Spacer()
                Button("フォルダを選択…") { dismiss(); chooseFolder() }
                    .disabled(isSelectingFolder)
                Menu("ライブラリ…") {
                    Button("ユーザライブラリ（~/Library）") {
                        dismiss(); chooseFolder(startingAt: .user)
                    }
                    Button("Macライブラリ（/Library）") {
                        dismiss(); chooseFolder(startingAt: .system)
                    }
                }
                .disabled(isSelectingFolder)
            }
            HStack {
                Text("APIサーバー").font(.caption).foregroundStyle(.secondary)
                if model.lanEnabled {
                    Label("LAN公開中", systemImage: "network")
                        .font(.caption).foregroundStyle(.orange)
                }
                Spacer()
                Button(model.apiURL) { model.copyAPIURL() }.buttonStyle(.link)
            }
            Divider()
            HStack {
                Button("設定…") { openSettings(); dismiss() }
                Spacer()
                Button("終了") { NSApplication.shared.terminate(nil) }
            }
        }
        .padding(14)
        .frame(width: 390)
        .task {
            while !Task.isCancelled {
                await model.refreshStatus()
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    private func chooseFolder(startingAt location: FileSelectionService.LibraryLocation? = nil) {
        isSelectingFolder = true
        Task { @MainActor in
            defer { isSelectingFolder = false }
            if let url = await FileSelectionService.shared.chooseFolder(startingAt: location?.url) {
                await model.addModelFolder(url)
            }
        }
    }
}
