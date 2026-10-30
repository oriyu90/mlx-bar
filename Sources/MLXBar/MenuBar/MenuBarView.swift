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

            // Resident-model preload (v3.2.0 additive `preload` in status).
            // A failed preload used to be a server-side warning only, so the
            // popover said "no model loaded" with no reason (ISS-3 / AC-3-3).
            // It is persistent state, not a transient error: it disappears when
            // the coordinator reports loaded/skipped, not on a 20-second timer.
            if let preload = model.preloadStatusLine {
                Label(preload.text, systemImage: preload.failed
                      ? "exclamationmark.triangle" : "arrow.down.circle")
                    .font(.caption)
                    .foregroundStyle(preload.failed ? Color.orange : Color.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(3)
            }

            // Model-state warnings from `status.loadedModel.warnings`
            // (v3.2.0 additive). They sit under the preload line because they
            // are the same kind of persistent state: an explicit mlx-lm load
            // of a vision-capable model drops attached images, and nothing
            // else tells the user. Same treatment as `cacheWarningText` —
            // orange, no background, no timer: the line goes away when the
            // coordinator stops reporting the warning.
            ForEach(model.modelWarnings, id: \.self) { warning in
                Label(warning.text(language: model.guiLanguage),
                      systemImage: warning.symbolName)
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(3)
                    .accessibilityIdentifier("model-warning-\(warning.code)")
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
                loadedModelsHeader
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
            if let adaptive = model.adaptiveMemorySummaryText {
                Label(adaptive, systemImage: "sparkles")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let retrieval = model.ragLastRetrievalText {
                Label(retrieval, systemImage: "text.magnifyingglass")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            // Settings recovered from a backup (v3.2.0): shown once per
            // recovery, and dismissible, because it explains a whole-reset that
            // otherwise looks like the app forgetting the user's settings.
            if let notice = model.settingsRecoveryNotice {
                HStack(alignment: .top, spacing: 6) {
                    Label(notice, systemImage: "arrow.counterclockwise.circle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .lineLimit(4)
                    Button { model.dismissSettingsRecoveryNotice() } label: {
                        Image(systemName: "xmark").font(.caption2)
                            .accessibilityLabel(LS("閉じる"))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
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
        // First-launch wizard (v2.6.0): the coordinator reports `required`
        // only on fresh installs, so updates never open this window.
        .onChange(of: model.showSetupWizard) { _, shown in
            if shown { open("setup") }
        }
    }

    /// "Loaded models · N", with the resident / non-resident split spelled out
    /// once the second tier exists (v2.8.0).
    ///
    /// Splitting the header keeps the distinction discoverable without adding a
    /// badge to every row: the badge is on the row (detail line), the numbers
    /// are here.
    private var loadedModelsHeader: some View {
        let count = displayRows.count
        let overflow = model.nonResidentModelCount
        let base = count > 1 ? "\(LS("ロード済みモデル")) · \(count)" : LS("ロード済みモデル")
        return Group {
            if overflow > 0 {
                Text(model.guiLanguage == "ja"
                     ? "\(base)（常駐 \(model.residentModelCount)・非常駐 \(overflow)）"
                     : "\(base) (\(model.residentModelCount) resident · \(overflow) non-resident)")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text(base).font(.caption).foregroundStyle(.secondary)
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
                // v2.8.0: a model loaded past `maxResidentModels` is marked here
                // so "why does this one not count?" is answerable in place.
                if !resident.resident && !isVirtualLoadingRow {
                    Text(LS("非常駐")).font(.caption2).foregroundStyle(.secondary)
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.15), in: Capsule())
                        .accessibilityLabel(LS("非常駐モデル"))
                }
                // ISS-7: only a saved residency profile is "固定". A model that
                // is loaded with `pin=True` (manual load) but has no profile is
                // kept alive for this session only, so it reads "一時" rather
                // than claiming a pin the user never set.
                if resident.keepLoaded && !resident.profilePinned && !isVirtualLoadingRow {
                    Text(LS("一時")).font(.caption2).foregroundStyle(.secondary)
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(Color.secondary.opacity(0.15), in: Capsule())
                        .accessibilityLabel(LS("一時"))
                }
                Spacer(minLength: 4)
                if resident.replicaCount > 1 {
                    Text("×\(resident.replicaCount)").font(.caption2).monospacedDigit()
                        .foregroundStyle(.secondary)
                        .accessibilityLabel(LS("並列数") + " \(resident.replicaCount)")
                }
                if !resident.managedExternally && !isVirtualLoadingRow {
                    Button {
                        Task { await model.setModelPin(resident.id, keepLoaded: !resident.profilePinned) }
                    } label: {
                        Image(systemName: resident.profilePinned ? "pin.fill" : "pin")
                            .accessibilityLabel(LS("常駐を維持"))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(resident.profilePinned ? Color.accentColor : Color.secondary)
                    // Until settings are read, there is no profile list to merge
                    // against; the server-side merge makes an empty snapshot
                    // harmless, but an operation with no known current state is
                    // still disabled rather than guessed (AC-7-4).
                    .disabled(!model.settingsLoaded || model.busy)
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
            // Per-row model-state warnings (A2). Without this line a warning
            // on a non-primary resident only exists in `loadedModels[]`, so
            // loading a second model made the first one's warning disappear
            // from the GUI even though the coordinator still reported it.
            ForEach(resident.warnings, id: \.self) { warning in
                Label(warning.text(language: model.guiLanguage),
                      systemImage: warning.symbolName)
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(3)
                    .padding(.leading, isVirtualLoadingRow ? 0 : 18)
            }
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
                // Non-resident idle countdown (v2.7.0, seconds-only since
                // v2.8.0): proves the configurable idle TTL is in effect for
                // this row and is the number the field below adds to.
                if let countdown = resident.idleCountdownText(japanese: model.guiLanguage == "ja") {
                    Text(countdown)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            .padding(.leading, isVirtualLoadingRow ? 0 : 18)
            // v2.8.0: extend this model's idle countdown by N seconds.
            if !isVirtualLoadingRow {
                IdleExtendField(resident: resident, isBusy: model.busy) { seconds in
                    await model.extendIdle(resident.id, seconds: seconds)
                }
                .padding(.leading, 18)
            }
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
        if resident.profilePinned { parts.append(LS("固定")) }
        else if resident.keepLoaded { parts.append(LS("一時")) }
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
