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
                        .font(.headline).lineLimit(2).fixedSize(horizontal: false, vertical: true)
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
                        Image(systemName: "doc.on.doc").accessibilityLabel(LS("モデル名をコピー"))
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
                if let rate = model.generationRateText {
                    Text(rate)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
                if let summary = model.cacheSummaryText {
                    Text(summary)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let warning = model.cacheWarningText {
                    // Wrapping is done with `frame(maxWidth:alignment:)` rather
                    // than `.fixedSize`, which blanks the Settings sidebar (see
                    // mlx-bar.md).
                    Label(warning, systemImage: "clock.badge.exclamationmark")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }

            if !model.residentModels.isEmpty {
                Divider()
                Text(model.residentModels.count > 1
                     ? "\(LS("常駐モデル")) · \(model.residentModels.count)"
                     : LS("常駐モデル"))
                    .font(.caption).foregroundStyle(.secondary)
                // Default max residency is 2 and the ceiling is 8; keep the
                // popover from growing without bound if it is raised.
                let rows = ForEach(model.residentModels) { resident in
                    residentRow(resident)
                }
                if model.residentModels.count > 4 {
                    ScrollView { VStack(alignment: .leading, spacing: 8) { rows } }
                        .frame(maxHeight: 180)
                } else {
                    rows
                }
            }

            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red).lineLimit(3)
            }
            Divider()
            Button(LS("モデルを選択…")) { open("models") }.keyboardShortcut("m")
            if model.loadedName == nil {
                Button(LS("クイックチャット…")) { open("chat") }.disabled(true)
            } else {
                Button(LS("クイックチャット…")) { open("chat") }.keyboardShortcut("n")
                Button(LS(model.residentModels.count > 1 ? "すべてアンロード" : "アンロード")) {
                    Task { await model.unload() }
                }
            }
            if model.currentRequestID != nil {
                Button(LS(model.cancellationInProgress ? "生成を停止処理中…" : "生成をキャンセル")) {
                    Task { await model.cancelGeneration() }
                }.disabled(model.cancellationInProgress)
            } else if model.activeRequestCount > 0 || model.queuedRequestCount > 0 {
                Button(LS(model.cancellationInProgress ? "すべて停止処理中…" : "すべての生成を停止")) {
                    Task { await model.cancelAllGenerations() }
                }.disabled(model.cancellationInProgress)
            }
            Divider()
            HStack {
                Button(LS("今すぐ再スキャン")) { Task { await model.scan() } }
                Spacer()
                Button(LS("フォルダを選択…")) { dismiss(); chooseFolder() }
                    .disabled(isSelectingFolder)
                Menu(LS("ライブラリ…")) {
                    Button(LS("ユーザライブラリ（~/Library）")) {
                        dismiss(); chooseFolder(startingAt: .user)
                    }
                    Button(LS("Macライブラリ（/Library）")) {
                        dismiss(); chooseFolder(startingAt: .system)
                    }
                }
                .disabled(isSelectingFolder)
            }
            HStack {
                Text(LS("APIサーバー")).font(.caption).foregroundStyle(.secondary)
                if model.lanEnabled {
                    Label(LS("LAN公開中"), systemImage: "network")
                        .font(.caption).foregroundStyle(.orange)
                }
                Spacer()
                Button(model.apiURL) { model.copyAPIURL() }
                    .buttonStyle(.link)
                    .accessibilityLabel(LS("接続URL"))
                    .accessibilityHint(LS("クリップボードへコピーします"))
            }
            Divider()
            HStack {
                Button(LS("設定…")) {
                    openSettings()
                    dismiss()
                    NSApplication.shared.activate(ignoringOtherApps: true)
                }
                Spacer()
                Button(LS("終了")) { NSApplication.shared.terminate(nil) }
            }
        }
        .padding(14)
        .frame(width: 420)
        .task {
            while !Task.isCancelled {
                await model.refreshStatus()
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    @ViewBuilder
    private func residentRow(_ resident: ResidentModel) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            VStack(alignment: .leading, spacing: 1) {
                Text(resident.name).font(.caption).lineLimit(1).truncationMode(.middle)
                Text(residentDetail(resident)).font(.caption2).foregroundStyle(.secondary)
                if let rate = resident.generationRateText(japanese: model.guiLanguage == "ja") {
                    Text(rate)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                        .accessibilityLabel(LS("生成速度") + " \(rate)")
                }
            }
            Spacer(minLength: 4)
            if resident.replicaCount > 1 {
                Text("×\(resident.replicaCount)").font(.caption2).monospacedDigit()
                    .foregroundStyle(.secondary)
                    .accessibilityLabel(LS("並列数") + " \(resident.replicaCount)")
            }
            if resident.activeLeases > 0 {
                Text(LS("使用中")).font(.caption2).foregroundStyle(.orange)
            }
            if !resident.managedExternally {
                Button {
                    Task { await model.setModelPin(resident.id, keepLoaded: !resident.keepLoaded) }
                } label: {
                    Image(systemName: resident.keepLoaded ? "pin.fill" : "pin")
                        .accessibilityLabel(LS("常駐を維持"))
                }
                .buttonStyle(.plain)
                .foregroundStyle(resident.keepLoaded ? Color.accentColor : Color.secondary)
                Button {
                    Task { await model.unloadModel(resident.id) }
                } label: {
                    Image(systemName: "eject").accessibilityLabel(LS("このモデルをアンロード"))
                }
                .buttonStyle(.plain)
                .disabled(model.busy)
            }
        }
    }

    private func residentDetail(_ resident: ResidentModel) -> String {
        var parts: [String] = []
        if let engine = resident.engine, !engine.isEmpty { parts.append(engine) }
        if resident.managedExternally {
            parts.append("LM Studio")
        } else if resident.poolState == "loading" {
            parts.append(LS("ロード中"))
        } else if resident.poolState == "evicting" {
            parts.append(LS("解放中"))
        }
        if let bytes = resident.memoryReservationBytes, bytes > 0 {
            parts.append(String(format: "%.1f GB", Double(bytes) / 1_073_741_824))
        }
        if resident.keepLoaded { parts.append(LS("固定")) }
        return parts.joined(separator: " · ")
    }

    // MLXBar has no Dock icon (LSUIElement), so opening a secondary window from
    // the menu bar extra's popover does not by itself make the app (or that
    // window) the key window — without this, the window appears but keystrokes
    // keep going to whichever app was frontmost before.
    private func open(_ id: String) {
        openWindow(id: id)
        dismiss()
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    private func chooseFolder(startingAt location: FileSelectionService.LibraryLocation? = nil) {
        isSelectingFolder = true
        Task { @MainActor in
            defer { isSelectingFolder = false }
            switch await FileSelectionService.shared.chooseFolder(startingAt: location?.url) {
            case .chosen(let urls):
                if let url = urls.first { await model.addModelFolder(url) }
            case .busy:
                model.errorMessage = LS("別のファイル選択画面が開いています。先にそちらを閉じてください。")
            case .cancelled:
                break
            }
        }
    }
}
