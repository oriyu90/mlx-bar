import SwiftUI

struct MenuBarView: View {
    @ObservedObject var model: MenuBarViewModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings
    @Environment(\.dismiss) private var dismiss
    @State private var isSelectingFolder = false

    /// A resident model plus a possible synthetic "still loading" row for a
    /// brand-new load that has no pool slot (and so no id) yet. v1.10.0
    /// replaces the old single-primary headline + separate "常駐モデル" list
    /// with one list where every resident model, including the one Quick Chat
    /// targets, is an equal row.
    private var displayRows: [ResidentModel] {
        var rows = model.residentModels
        if let loadingName = model.loadingModelName,
           !rows.contains(where: { $0.name == loadingName && $0.poolState == "loading" }) {
            var descriptor: [String: Any] = ["id": "__loading__:\(loadingName)",
                                             "name": loadingName, "poolState": "loading"]
            if let engine = model.loadingEngine { descriptor["engine"] = engine }
            if let virtual = ResidentModel(descriptor) { rows.insert(virtual, at: 0) }
        }
        return rows
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: model.icon).font(.title2)
                VStack(alignment: .leading) {
                    Text(model.serviceStatus)
                        .font(.headline)
                    Text(model.apiURL).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
            }

            if let status = model.modelCopyStatus {
                Text(status).font(.caption).foregroundStyle(.secondary)
            }

            if model.loadedName != nil {
                Text("Max token \(MenuBarViewModel.tokenCount(model.effectiveMaxTokens))")
                    .font(.caption).foregroundStyle(.secondary)
            }

            if displayRows.isEmpty {
                Text(LS("モデル未ロード")).font(.caption).foregroundStyle(.secondary)
            } else {
                Text(displayRows.count > 1
                     ? "\(LS("ロード済みモデル")) · \(displayRows.count)"
                     : LS("ロード済みモデル"))
                    .font(.caption).foregroundStyle(.secondary)
                // Default max residency is 2 and the ceiling is 8; keep the
                // popover from growing without bound if it is raised.
                let rows = ForEach(displayRows) { resident in
                    modelRow(resident, isPrimary: resident.name == model.loadedName)
                }
                if displayRows.count > 4 {
                    ScrollView { VStack(alignment: .leading, spacing: 10) { rows } }
                        .frame(maxHeight: 220)
                } else {
                    VStack(alignment: .leading, spacing: 10) { rows }
                }
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
            if let compression = model.contextCompressionSummaryText {
                Label(compression, systemImage: "arrow.down.right.and.arrow.up.left")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
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

    /// One row of the v1.10.0 unified model list: name (with a left-side copy
    /// button so any resident model's name can be copied, not just the
    /// primary one), the existing engine/size/replica detail line, and a
    /// status line that always shows loading/generating/queued/idle and adds
    /// live tok/s only while actually generating.
    @ViewBuilder
    private func modelRow(_ resident: ResidentModel, isPrimary: Bool) -> some View {
        let isVirtualLoadingRow = resident.id.hasPrefix("__loading__:")
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                if !isVirtualLoadingRow {
                    Button { model.copyModelName(resident.name) } label: {
                        Image(systemName: "doc.on.doc").font(.caption2)
                            .accessibilityLabel(LS("モデル名をコピー"))
                    }.buttonStyle(.plain)
                }
                Text(resident.name).font(.caption).bold().lineLimit(1).truncationMode(.middle)
                if isPrimary && !isVirtualLoadingRow {
                    Text(LS("既定")).font(.caption2).foregroundStyle(.secondary)
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.15), in: Capsule())
                        .accessibilityLabel(LS("クイックチャットの既定モデル"))
                }
                Spacer(minLength: 4)
                if resident.replicaCount > 1 {
                    Text("×\(resident.replicaCount)").font(.caption2).monospacedDigit()
                        .foregroundStyle(.secondary)
                        .accessibilityLabel(LS("並列数") + " \(resident.replicaCount)")
                }
                if !resident.managedExternally && !isVirtualLoadingRow {
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
            Text(residentDetail(resident)).font(.caption2).foregroundStyle(.secondary)
                .padding(.leading, isVirtualLoadingRow ? 0 : 18)
            HStack(spacing: 6) {
                let symbol = resident.activitySymbol
                Label(resident.activityText(japanese: model.guiLanguage == "ja"), systemImage: symbol.name)
                    .font(.caption2)
                    .foregroundStyle(symbol.tint)
                if let rate = resident.generationRateText(japanese: model.guiLanguage == "ja") {
                    Text(rate)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                        .accessibilityLabel(LS("生成速度") + " \(rate)")
                }
            }
            .padding(.leading, isVirtualLoadingRow ? 0 : 18)
            if isVirtualLoadingRow, let phase = model.loadingPhase {
                Text(phase).font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private func residentDetail(_ resident: ResidentModel) -> String {
        var parts: [String] = []
        if let engine = resident.engine, !engine.isEmpty { parts.append(engine) }
        if resident.managedExternally {
            parts.append("LM Studio")
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
