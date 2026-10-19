import SwiftUI

private struct RuntimeDeletionTarget: Identifiable {
    let engine: String
    let slot: String
    let version: String?
    let isPrevious: Bool
    var id: String { "\(engine):\(slot)" }
}

struct RuntimeManagerView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var versions: [String: String] = ["mlx-lm": "", "mlx-vlm": ""]
    @State private var deletionTarget: RuntimeDeletionTarget?

    var body: some View {
        List {
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
            }
            Section {
                RuntimeSettingsSection(model: model)
            } header: {
                Label(LS("更新の設定"), systemImage: "gearshape")
            }
            Section {
                OMLXSetupSection(model: model)
            } header: {
                Label(LS("oMLXサーバー"), systemImage: "server.rack")
            } footer: {
                Text(LS("oMLXは外部のOpenAI互換サーバーです。ここからHomebrewでインストールし、起動・停止できます。"))
            }
            ForEach(model.runtimes) { runtime in
                Section {
                    runtimeSummary(runtime)
                    updateControls(runtime)
                    advancedControls(runtime)
                    history(runtime)
                } header: {
                    Label(runtime.id.uppercased(), systemImage: "shippingbox")
                }
            }
        }
        .navigationTitle(LS("ランタイム"))
        .task { await model.refreshRuntimeManager() }
        .confirmationDialog(LS("以前のランタイムを削除しますか？"),
                            isPresented: Binding(
                                get: { deletionTarget != nil },
                                set: { if !$0 { deletionTarget = nil } }
                            ), presenting: deletionTarget) { target in
            Button(LS("削除"), role: .destructive) {
                deletionTarget = nil
                Task { await model.deleteRuntimeSlot(target.engine, slot: target.slot, version: target.version) }
            }
            Button(LS("キャンセル"), role: .cancel) { deletionTarget = nil }
        } message: { target in
            Text("\(target.engine.uppercased()) \(target.version ?? "") — \(LS("を削除します。")) \(LS(target.isPrevious ? "この版は復元先として登録されています。削除後はこの版へ戻せません。" : "この操作は取り消せません。"))")
        }
    }

    @ViewBuilder
    private func runtimeSummary(_ runtime: RuntimeInfo) -> some View {
        LabeledContent(LS("現在のバージョン"), value: runtime.activeVersion ?? LS("未インストール"))
        if let active = runtime.active {
            LabeledContent(LS("保存領域ID"), value: active)
                .font(.caption).foregroundStyle(.secondary)
        }
        if let check = model.runtimeUpdates[runtime.id] {
            LabeledContent(LS("最新安定版"), value: check.candidateVersion ?? LS("取得できません"))
            Label(updateStatusText(runtime, check), systemImage: updateStatusIcon(check))
                .foregroundStyle(check.updateAvailable ? .blue : .green)
            if let checkedAt = check.checkedAt {
                Text("\(LS("最終確認")): \(checkedAt.formatted(date: .abbreviated, time: .standard))")
                    .font(.caption).foregroundStyle(.secondary)
            }
        } else {
            Text(LS("最新版は未確認です"))
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func updateControls(_ runtime: RuntimeInfo) -> some View {
        let updating = model.updatingEngines.contains(runtime.id)
        let checking = model.checkingRuntimeEngines.contains(runtime.id)
        HStack {
            Button(LS("更新を確認")) { Task { await model.checkRuntimeUpdate(runtime.id) } }
                .disabled(updating || checking)
                .accessibilityLabel("\(runtime.id) — \(LS("更新を確認"))")
            Button(updateButtonTitle(runtime)) {
                Task { await model.updateRuntimeAutomatically(runtime.id) }
            }
            .buttonStyle(.borderedProminent)
            .disabled(updating || checking || isConfirmedLatest(runtime))
            .accessibilityLabel("\(runtime.id) — \(LS("最新版へ自動更新"))")
            Spacer()
            if checking { ProgressView().controlSize(.small) }
        }
        if let progress = model.runtimeProgress[runtime.id] {
            Text(progress).font(.caption).foregroundStyle(.secondary)
        }
        if let job = model.runtimeJobs[runtime.id] {
            runtimeJobCard(runtime.id, job: job)
        }
        Text(LS("新しい環境へ取得・検証してから切り替えます。失敗時は状態を確認し、安全に戻せる場合だけ以前の版へ復元します。"))
            .font(.caption).foregroundStyle(.secondary)
    }

    private func isConfirmedLatest(_ runtime: RuntimeInfo) -> Bool {
        guard runtime.active != nil, let check = model.runtimeUpdates[runtime.id] else { return false }
        return !check.updateAvailable
    }

    private func updateButtonTitle(_ runtime: RuntimeInfo) -> String {
        if model.guiLanguage == "ja" {
            if runtime.active == nil { return "最新版をインストール" }
            if model.runtimeUpdates[runtime.id]?.versionStatus == "newer_than_stable" { return "新しい版を使用中" }
            return isConfirmedLatest(runtime) ? "最新版を使用中" : "最新版へ自動更新"
        }
        if runtime.active == nil { return "Install Latest" }
        if model.runtimeUpdates[runtime.id]?.versionStatus == "newer_than_stable" { return "Using Newer Version" }
        return isConfirmedLatest(runtime) ? "Up to Date" : "Update to Latest"
    }

    private func updateStatusText(_ runtime: RuntimeInfo, _ check: RuntimeUpdateInfo) -> String {
        if model.guiLanguage == "ja" {
            if runtime.active == nil || check.versionStatus == "not_installed" { return "最新版をインストールできます" }
            if check.versionStatus == "newer_than_stable" { return "安定版より新しい版を使用中です" }
            return check.updateAvailable ? "更新できます" : "最新版です"
        }
        if runtime.active == nil || check.versionStatus == "not_installed" { return "Latest version can be installed" }
        if check.versionStatus == "newer_than_stable" { return "Using a version newer than stable" }
        return check.updateAvailable ? "Update available" : "Up to date"
    }

    private func updateStatusIcon(_ check: RuntimeUpdateInfo) -> String {
        check.updateAvailable ? "arrow.down.circle.fill" : "checkmark.circle.fill"
    }

    @ViewBuilder
    private func runtimeJobCard(_ engine: String, job: RuntimeJobInfo) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                if job.isActive {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: job.isFailed ? "xmark.circle.fill" : (job.isCancelled ? "stop.circle.fill" : "checkmark.circle.fill"))
                        .foregroundStyle(job.isFailed ? Color.red : (job.isCancelled ? Color.orange : Color.green))
                }
                Text(job.isActive
                     ? (model.guiLanguage == "ja" ? job.operationName : (job.kind.hasPrefix("runtime_stage:") ? "Download and verify runtime" : "Update runtime"))
                     : (job.isFailed ? (model.guiLanguage == "ja" ? "処理に失敗しました" : "Operation Failed")
                        : (job.isCancelled ? (model.guiLanguage == "ja" ? "処理を中止しました" : "Operation Cancelled")
                           : (model.guiLanguage == "ja" ? "処理が完了しました" : "Operation Completed"))))
                    .font(.headline)
                Spacer()
                if let progress = job.progress {
                    Text("\(Int(min(max(progress, 0), 1) * 100))%")
                        .monospacedDigit().foregroundStyle(.secondary)
                }
            }
            if let progress = job.progress {
                ProgressView(value: min(max(progress, 0), 1))
            }
            Text(job.isFailed ? (job.errorMessage ?? runtimeMessage(job.message)) : runtimeMessage(job.message))
                .font(.caption)
                .foregroundStyle(job.isFailed ? .red : .secondary)
            if job.isActive {
                Button(LS("処理を中止"), role: .destructive) {
                    Task { await model.cancelRuntimeJob(engine) }
                }
                .accessibilityLabel("\(engine) — \(LS("処理を中止"))")
            }
            if job.errorCode == "ROLLBACK_FAILED" {
                Label(LS("旧ランタイムへの復元にも失敗しました"), systemImage: "exclamationmark.octagon.fill")
                    .font(.caption).foregroundStyle(.red)
            } else if job.errorCode == "JOB_INTERRUPTED" {
                Text(LS("サービス再起動により中断されました。再実行してください。"))
                    .font(.caption).foregroundStyle(.orange)
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }

    @ViewBuilder
    private func advancedControls(_ runtime: RuntimeInfo) -> some View {
        let updating = model.updatingEngines.contains(runtime.id)
        let canRollback = runtime.slots.contains { ($0["previous"] as? Bool) == true }
        DisclosureGroup(LS("詳細・手動操作")) {
            HStack {
                TextField(LS("指定バージョン"), text: Binding(
                    get: { versions[runtime.id] ?? "" },
                    set: { versions[runtime.id] = $0 }
                ))
                Button(LS("指定版をダウンロード・検証")) {
                    Task { await model.stageRuntime(runtime.id, version: versions[runtime.id]) }
                }
                .disabled(updating || model.busy)
                .accessibilityLabel("\(runtime.id) — \(LS("指定版をダウンロード・検証"))")
                Button(LS("前の版へ戻す")) { Task { await model.rollback(runtime.id) } }
                    .disabled(!canRollback || updating || model.busy)
                    .accessibilityLabel("\(runtime.id) — \(LS("前の版へ戻す"))")
            }
            ForEach(runtime.slots, id: \.slotIdentifier) { slot in
                slotRow(runtime, slot: slot)
            }
        }
    }

    @ViewBuilder
    private func slotRow(_ runtime: RuntimeInfo, slot: [String: Any]) -> some View {
        let active = (slot["active"] as? Bool) == true
        let previous = (slot["previous"] as? Bool) == true
        let id = slot["id"] as? String ?? "unknown"
        let version = (slot["probe"] as? [String: Any])?["version"] as? String
        HStack {
            Image(systemName: active ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(active ? .green : .secondary)
            VStack(alignment: .leading) {
                Text(version.map { "\(LS("バージョン")) \($0)" } ?? LS("バージョン不明"))
                Text(id).font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if active { Text(LS("使用中")).font(.caption).foregroundStyle(.green) }
            else if previous { Text(LS("ロールバック用")).font(.caption).foregroundStyle(.orange) }
            Spacer()
            if !active {
                Button(LS("切替")) { Task { await model.activate(runtime.id, slot: id) } }
                    .disabled(model.busy || model.updatingEngines.contains(runtime.id))
                    .accessibilityLabel("\(runtime.id) \(version ?? LS("不明")) — \(LS("切替"))")
                Button(LS("削除…"), role: .destructive) {
                    deletionTarget = RuntimeDeletionTarget(engine: runtime.id, slot: id,
                                                           version: version, isPrevious: previous)
                }
                .disabled(model.busy || model.updatingEngines.contains(runtime.id))
                .accessibilityLabel("\(runtime.id) \(version ?? LS("不明")) — \(LS("削除"))")
            }
        }
    }

    @ViewBuilder
    private func history(_ runtime: RuntimeInfo) -> some View {
        if !runtime.history.isEmpty {
            DisclosureGroup(LS("更新履歴")) {
                ForEach(runtime.history, id: \.historyIdentifier) { item in
                    historyRow(item)
                }
            }
        }
    }

    @ViewBuilder
    private func historyRow(_ item: [String: Any]) -> some View {
        let action = item["action"] as? String ?? "unknown"
        let result = item["result"] as? [String: Any] ?? [:]
        let version = (result["version"] as? String)
            ?? ((result["probe"] as? [String: Any])?["version"] as? String)
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Label(historyTitle(action), systemImage: historyIcon(action))
                    .foregroundStyle(action == "failed" ? .red : .primary)
                if let version { Text(version).font(.caption).foregroundStyle(.secondary) }
                Spacer()
                Text(item["created_at"] as? String ?? "")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if action == "failed", let message = result["message"] as? String {
                Text(message).font(.caption).foregroundStyle(.red).lineLimit(3)
                if result["rolledBack"] as? Bool == true {
                    Text(LS("旧ランタイムへ復元済み")).font(.caption).foregroundStyle(.secondary)
                }
            }
            Text("\(LS("保存領域ID")): \(item["slot_id"] as? String ?? "")")
                .font(.caption2).foregroundStyle(.tertiary).textSelection(.enabled)
        }
    }

    private func historyTitle(_ action: String) -> String {
        if model.guiLanguage != "ja" {
            switch action {
            case "staged": return "Download and Verification Complete"
            case "activated": return "Update Activated"
            case "failed": return "Update Failed"
            case "cancelled": return "Update Cancelled"
            case "deleted": return "Runtime Removed"
            default: return action
            }
        }
        switch action {
        case "staged": return "ダウンロード・検証完了"
        case "activated": return "更新・切替完了"
        case "failed": return "更新失敗"
        case "cancelled": return "更新中止"
        case "deleted": return "ランタイム削除"
        default: return action
        }
    }

    private func historyIcon(_ action: String) -> String {
        switch action {
        case "staged": "checkmark.shield"
        case "activated": "arrow.triangle.2.circlepath.circle.fill"
        case "failed": "xmark.octagon.fill"
        case "cancelled": "stop.circle.fill"
        case "deleted": "trash"
        default: "clock"
        }
    }

    private func runtimeMessage(_ value: String) -> String {
        guard model.guiLanguage != "ja" else { return value }
        let exact = [
            "待機中": "Queued", "開始しています": "Starting", "完了": "Completed", "失敗": "Failed",
            "最新版を確認中": "Checking latest version", "新しいPython環境を作成中": "Creating a new Python environment",
            "依存関係を検証中": "Verifying dependencies", "アダプター互換性を検証中": "Verifying adapter compatibility",
            "新しいslotの検証が完了": "New runtime slot verified", "実行中の生成が終わるのを待っています": "Waiting for active generation to finish",
            "新しいランタイムへ切替中": "Activating the new runtime", "切替後のワーカーを確認中": "Checking the new worker",
            "使用中モデルを再ロード中": "Reloading the active model", "更新が完了しました": "Update completed",
        ]
        if let translated = exact[value] { return translated }
        if value.contains("をダウンロード・インストール中") {
            return value.replacingOccurrences(of: "をダウンロード・インストール中", with: " — downloading and installing")
                .replacingOccurrences(of: "秒経過", with: "s elapsed")
        }
        return value
    }
}

/// oMLX external-server setup card (v2.10.0).
///
/// Reads `GET /omlx/setup/requirements` through the view model and drives
/// `POST /omlx/setup/{install,start,stop}` as jobs — the same 202 + jobs
/// plumbing the runtime cards use. Install state and the CLI probe are the
/// server's words (`omlx_setup.py`); the card only decides what is tappable.
private struct OMLXSetupSection: View {
    @ObservedObject var model: MenuBarViewModel

    private static let actions = ["install", "start", "stop"]

    private var cli: [String: Any] { model.omlxRequirements["cli"] as? [String: Any] ?? [:] }
    private var brew: [String: Any] { model.omlxRequirements["brew"] as? [String: Any] ?? [:] }
    private var installed: Bool { model.omlxRequirements["installed"] as? Bool ?? false }
    private var brewFound: Bool { brew["found"] as? Bool ?? false }
    private var anyJobActive: Bool { model.omlxSetupJobs.values.contains { $0.isActive } }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.omlxRequirements.isEmpty {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(LS("oMLXの状態を確認しています…")).foregroundStyle(.secondary)
                }
            } else {
                statusRow
                actions
            }
            ForEach(Self.actions, id: \.self) { action in
                if let job = model.omlxSetupJobs[action] {
                    omlxJobCard(action, job: job)
                }
            }
        }
        .task { await model.refreshOMLXSetupRequirements() }
    }

    @ViewBuilder
    private var statusRow: some View {
        HStack(spacing: 8) {
            Image(systemName: installed ? "checkmark.circle.fill" : "circle.dashed")
                .foregroundStyle(installed ? Color.green : .secondary)
            if installed {
                VStack(alignment: .leading, spacing: 2) {
                    Text(LS("oMLXをインストール済み")).font(.headline)
                    if let version = cli["version"] as? String, !version.isEmpty {
                        Text(version).font(.caption).foregroundStyle(.secondary)
                    }
                }
            } else {
                Text(LS("oMLXはインストールされていません")).font(.headline)
            }
            Spacer()
        }
    }

    @ViewBuilder
    private var actions: some View {
        if !installed {
            if brewFound {
                VStack(alignment: .leading, spacing: 6) {
                    Button(LS("Homebrewでインストール")) {
                        Task { await model.startOMLXSetup("install") }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(anyJobActive || model.omlxBusySetupActions.contains("install"))
                    .accessibilityLabel(LS("oMLX — Homebrewでインストール"))
                    if let command = brew["installCommand"] as? String, !command.isEmpty {
                        Text(command).font(.caption2).foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Label(LS("Homebrewが見つからないため、ここからはインストールできません"),
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    if let releases = model.omlxRequirements["releasesUrl"] as? String,
                       !releases.isEmpty {
                        Link(LS("oMLXのリリースページを開く"), destination: URL(string: releases) ?? URL(string: "https://github.com/jundot/omlx/releases")!)
                            .font(.caption)
                    }
                }
            }
        } else {
            HStack(spacing: 10) {
                Button(LS("サーバーを起動")) {
                    Task { await model.startOMLXSetup("start") }
                }
                .buttonStyle(.borderedProminent)
                .disabled(anyJobActive)
                .accessibilityLabel(LS("oMLX — サーバーを起動"))
                Button(LS("サーバーを停止")) {
                    Task { await model.startOMLXSetup("stop") }
                }
                .disabled(anyJobActive)
                .accessibilityLabel(LS("oMLX — サーバーを停止"))
            }
        }
    }

    @ViewBuilder
    private func omlxJobCard(_ action: String, job: RuntimeJobInfo) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                if job.isActive {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: job.isFailed ? "xmark.circle.fill"
                          : (job.isCancelled ? "stop.circle.fill" : "checkmark.circle.fill"))
                        .foregroundStyle(job.isFailed ? Color.red
                                         : (job.isCancelled ? Color.orange : Color.green))
                }
                Text(headline(action, job: job)).font(.headline)
                Spacer()
                if let progress = job.progress {
                    Text("\(Int(min(max(progress, 0), 1) * 100))%")
                        .monospacedDigit().foregroundStyle(.secondary)
                }
            }
            if let progress = job.progress, job.isActive {
                ProgressView(value: min(max(progress, 0), 1))
            }
            Text(job.isFailed ? (job.errorMessage ?? job.message) : message(job.message))
                .font(.caption)
                .foregroundStyle(job.isFailed ? .red : .secondary)
            if job.isActive {
                Button(LS("処理を中止"), role: .destructive) {
                    Task { await model.cancelOMLXSetup(action) }
                }
                .accessibilityLabel(LS("oMLX — 処理を中止"))
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }

    private func headline(_ action: String, job: RuntimeJobInfo) -> String {
        let japanese = model.guiLanguage == "ja"
        if !job.isActive {
            if japanese { return job.isFailed ? "処理に失敗しました" : (job.isCancelled ? "処理を中止しました" : "処理が完了しました") }
            return job.isFailed ? "Operation Failed" : (job.isCancelled ? "Operation Cancelled" : "Operation Completed")
        }
        if japanese { return job.operationName }
        switch job.kind {
        case "omlx_install": return "Install oMLX"
        case "omlx_start": return "Start the oMLX server"
        case "omlx_stop": return "Stop the oMLX server"
        default: return job.operationName
        }
    }

    /// The setup service reports in Japanese only; translate the recurring
    /// lines for an English interface (same approach as `runtimeMessage`).
    private func message(_ value: String) -> String {
        guard model.guiLanguage != "ja" else { return value }
        let exact = [
            "待機中": "Queued",
            "開始しています": "Starting",
            "完了": "Completed",
            "失敗": "Failed",
            "サーバーの応答を待っています": "Waiting for the server to respond",
            "oMLXのインストールが完了しました": "oMLX installation completed",
            "oMLXサーバーを起動しました": "oMLX server started",
            "oMLXサーバーを起動しました（設定したURLにはまだ到達できません）":
                "oMLX server started (the configured URL is not reachable yet)",
            "oMLXサーバーの停止コマンドを実行しました": "The oMLX stop command completed",
            // v2.10.1: the remaining job-framework messages (cancel + terminal
            // states) were leaking to English users in Japanese.
            "利用者が処理を中止しました": "Operation cancelled by the user",
            "処理に失敗しました": "The operation failed",
            "処理が完了しました": "The operation completed",
        ]
        if let translated = exact[value] { return translated }
        if value.contains("をダウンロード・インストール中（") {
            return value
                .replacingOccurrences(of: "oMLXをダウンロード・インストール中（", with: "Downloading and installing oMLX (")
                .replacingOccurrences(of: "秒経過）", with: "s elapsed)")
        }
        if value.contains("HomebrewでoMLXをインストールしています") {
            return value.replacingOccurrences(
                of: "HomebrewでoMLXをインストールしています", with: "Installing oMLX via Homebrew")
        }
        if value.contains("oMLXサーバーを起動しています") {
            return value.replacingOccurrences(
                of: "oMLXサーバーを起動しています", with: "Starting the oMLX server")
        }
        if value.contains("oMLXサーバーを停止しています") {
            return value.replacingOccurrences(
                of: "oMLXサーバーを停止しています", with: "Stopping the oMLX server")
        }
        if value.contains("omlxコマンドを実行中（") {
            return value
                .replacingOccurrences(of: "omlxコマンドを実行中（", with: "Running the omlx command (")
                .replacingOccurrences(of: "秒経過）", with: "s elapsed)")
        }
        return value
    }
}

/// Stable `ForEach` identities for slot/history rows.
///
/// Both `ForEach`s previously keyed on `.indices`, which ties SwiftUI's
/// diffing to array position rather than content: an insert, removal, or
/// reorder (e.g. a runtime job finishing and the slot list refreshing) can
/// make SwiftUI reuse a row's identity — and any per-row state or in-flight
/// animation — for what is actually a different slot/history entry. Keying on
/// the backend's own identifiers keeps a row tied to the same real record
/// across refreshes.
private extension [String: Any] {
    var slotIdentifier: String { self["id"] as? String ?? "unknown" }

    var historyIdentifier: String {
        let createdAt = self["created_at"] as? String ?? ""
        let action = self["action"] as? String ?? ""
        let slotID = self["slot_id"] as? String ?? ""
        return "\(createdAt)|\(action)|\(slotID)"
    }
}

/// Update channel + auto-check settings (v2.6.0).
///
/// Display and mutation for the `runtimes.*` keys, which previously had
/// neither a GUI nor a named CLI command (`config set` only). Ranges mirror
/// `config set-runtime`; the server re-validates.
private struct RuntimeSettingsSection: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var checkInterval = ""
    @State private var keepSlots = ""

    private var runtimes: [String: Any] { model.settings["runtimes"] as? [String: Any] ?? [:] }

    private func engine(_ name: String) -> [String: Any] {
        runtimes[name] as? [String: Any] ?? [:]
    }

    var body: some View {
        Toggle(LS("mlx-lm の更新を自動確認"), isOn: Binding(
            get: { engine("mlx-lm")["autoCheck"] as? Bool ?? false },
            set: { value in Task { await model.setConfig("runtimes.mlx-lm.autoCheck", value: value) } }
        ))
        Toggle(LS("mlx-vlm の更新を自動確認"), isOn: Binding(
            get: { engine("mlx-vlm")["autoCheck"] as? Bool ?? false },
            set: { value in Task { await model.setConfig("runtimes.mlx-vlm.autoCheck", value: value) } }
        ))
        Toggle(LS("不足ランタイムを自動導入"), isOn: Binding(
            get: { runtimes["autoInstallMissing"] as? Bool ?? true },
            set: { value in Task { await model.setConfig("runtimes.autoInstallMissing", value: value) } }
        ))
        HStack {
            Text(LS("更新確認の間隔（時間）"))
            Spacer()
            TextField("168", text: $checkInterval)
                .frame(width: 80).multilineTextAlignment(.trailing)
            Button(LS("適用")) {
                if let hours = Int(checkInterval) {
                    Task { await model.setConfig("runtimes.checkIntervalHours", value: hours) }
                }
            }
        }
        HStack {
            Text(LS("保持する世代数"))
            Spacer()
            TextField("3", text: $keepSlots)
                .frame(width: 80).multilineTextAlignment(.trailing)
            Button(LS("適用")) {
                if let slots = Int(keepSlots) {
                    Task { await model.setConfig("runtimes.keepSlots", value: slots) }
                }
            }
        }
        Text(LS("更新チャンネルは安定版（stable）固定です。無効な値はサーバー側で拒否されます。"))
            .font(.caption).foregroundStyle(.secondary)
            .onAppear {
                if checkInterval.isEmpty, let hours = runtimes["checkIntervalHours"] {
                    checkInterval = "\(hours)"
                }
                if keepSlots.isEmpty, let slots = runtimes["keepSlots"] {
                    keepSlots = "\(slots)"
                }
            }
    }
}
