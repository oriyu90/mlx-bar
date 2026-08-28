import SwiftUI

struct MLXBarSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var selection = "APIサーバー"
    /// The Japanese titles double as localization keys, so the selection stays
    /// valid when the interface language changes underneath it.
    private let pages = ["一般", "モデル", "APIサーバー", "LM Studio", "ランタイム", "キャッシュ", "詳細", "削除"]

    var body: some View {
        NavigationSplitView {
            List(pages, id: \.self, selection: $selection) { page in
                Text(LS(page))
            }
        } detail: {
            switch selection {
            case "一般": GeneralSettingsView(model: model)
            case "モデル": ModelSourceSettingsView(model: model)
            case "APIサーバー": APISettingsView(model: model)
            case "LM Studio": LMStudioSettingsView(model: model)
            case "ランタイム": RuntimeManagerView(model: model)
            case "キャッシュ": PromptCacheSettingsView(model: model)
            case "詳細": DiagnosticsSettingsView(model: model)
            case "削除": DataRemovalSettingsView(model: model)
            default: EmptyView()
            }
        }
        .onAppear { NSApplication.shared.activate(ignoringOtherApps: true) }
        .task { await model.refreshSettings(); await model.refreshSecrets() }
    }
}

struct PromptCacheSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var diskEnabled = true
    @State private var diskMaxGB = 5
    @State private var confirmsDiskClear = false

    private var configured: [String: Any] {
        model.settings["promptCache"] as? [String: Any] ?? [:]
    }

    private func bytes(_ value: Any?) -> String {
        let number = (value as? NSNumber)?.int64Value ?? 0
        return ByteCountFormatter.string(fromByteCount: number, countStyle: .file)
    }

    var body: some View {
        Form {
            Section(LS("永続プロンプトキャッシュ")) {
                Toggle(LS("再起動後もプロンプトキャッシュを再利用"), isOn: $diskEnabled)
                Stepper("\(LS("最大ディスク容量")): \(diskMaxGB) GB", value: $diskMaxGB, in: 1...100)
                Button(LS("設定を適用")) {
                    Task { await model.setPromptCacheSettings(enabled: diskEnabled, maximumGB: diskMaxGB) }
                }.buttonStyle(.borderedProminent)
                Text(LS("ZCodeの長いsystem promptとtools定義をローカルディスクへ保存します。設定変更は次回のモデルWorker起動時に反映されます。"))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section(LS("キャッシュ状態")) {
                LabeledContent(LS("メモリー"), value: (model.promptCacheStatus["memory"] as? Bool) == true ? LS("有効") : LS("無効"))
                LabeledContent(LS("ディスク"), value: (model.promptCacheStatus["disk"] as? Bool) == true ? LS("有効") : LS("無効"))
                LabeledContent(LS("ディスク使用量"), value: bytes(model.promptCacheStatus["disk_bytes"]))
                LabeledContent(LS("ディスクヒット"), value: "\((model.promptCacheStatus["disk_hits"] as? NSNumber)?.intValue ?? 0)")
                if let reason = model.promptCacheStatus["disabledReason"] as? String {
                    Text("\(LS("無効理由")): \(reason)").font(.caption).foregroundStyle(.orange)
                }
                if let summary = model.cacheSummaryText {
                    LabeledContent(LS("再利用方式"), value: summary)
                }
                if model.cacheAffordableTokens > 0 {
                    LabeledContent(LS("保存できる長さ"),
                                   value: "\(model.cacheAffordableTokens.formatted()) tokens")
                }
                if let cold = model.cacheLastColdReason {
                    LabeledContent(LS("直近の再利用なしの理由"),
                                   value: MenuBarViewModel.cacheReasonText(cold,
                                                                           japanese: model.guiLanguage == "ja"))
                }
                if let warning = model.cacheWarningText {
                    Text(warning).font(.caption).foregroundStyle(.orange)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack {
                    Button(LS("更新")) { Task { await model.refreshPromptCache() } }
                    Button(LS("メモリーキャッシュを消去")) {
                        Task { await model.clearPromptCache(memory: true) }
                    }
                    Button(LS("ディスクキャッシュを消去…"), role: .destructive) {
                        confirmsDiskClear = true
                    }
                }
                if let message = model.promptCacheMessage {
                    Text(message).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .padding()
        .task {
            await model.refreshSettings()
            diskEnabled = configured["diskEnabled"] as? Bool ?? true
            diskMaxGB = (configured["diskMaxGB"] as? NSNumber)?.intValue ?? 5
            await model.refreshPromptCache()
        }
        .confirmationDialog(LS("ディスクキャッシュを消去しますか？"), isPresented: $confirmsDiskClear) {
            Button(LS("ディスクキャッシュを消去"), role: .destructive) {
                Task { await model.clearPromptCache(memory: false) }
            }
            Button(LS("キャンセル"), role: .cancel) {}
        } message: {
            Text(LS("次の会話ではプロンプトを再計算します。モデルや会話履歴は削除しません。"))
        }
    }
}

struct DiagnosticsSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var confirmsClear = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(LS("最近のAPIログ")).font(.headline)
                Spacer()
                Button(LS("更新")) { Task { await model.refreshRecentLogs() } }
                Button(LS("コピー")) { model.copyRecentLogs() }.disabled(model.recentLogs.isEmpty)
                Button(LS("消去…"), role: .destructive) { confirmsClear = true }.disabled(model.recentLogs.isEmpty)
            }
            Text(LS("リクエスト本文、応答本文、APIキーは記録しません。最新2,000件だけを保存し、この画面には500件まで表示します。"))
                .font(.caption).foregroundStyle(.secondary)
            if let status = model.logStatus { Text(status).font(.caption).foregroundStyle(.secondary) }
            if model.recentLogs.isEmpty {
                ContentUnavailableView(LS("ログはありません"), systemImage: "doc.text.magnifyingglass")
            } else {
                List(Array(model.recentLogs.enumerated()), id: \.offset) { entry in
                    Text(MenuBarViewModel.formatLog(entry.element))
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
            }
        }
        .padding()
        .task { await model.refreshRecentLogs() }
        .confirmationDialog(LS("APIログを消去しますか？"), isPresented: $confirmsClear) {
            Button(LS("ログを消去"), role: .destructive) { Task { await model.clearRecentLogs() } }
            Button(LS("キャンセル"), role: .cancel) {}
        } message: { Text(LS("この操作は取り消せません。モデルや設定には影響しません。")) }
    }
}

struct DataRemovalSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var confirmsRemoval = false

    var body: some View {
        Form {
            Section(LS("MLXBarのデータを削除")) {
                Text(LS("設定、履歴、APIキー、MLXBarがダウンロードしたランタイム、ログをこのMacから削除します。"))
                Text(LS("Hugging FaceやLM Studioなど、外部のモデルフォルダにあるモデル本体は削除しません。"))
                    .font(.caption).foregroundStyle(.secondary)
                if model.isRemovingAllData {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text(LS("サービスを停止してデータを削除中…"))
                    }
                }
                Button(LS("すべてのデータを削除して終了…"), role: .destructive) {
                    confirmsRemoval = true
                }
                .disabled(model.isRemovingAllData)
                .accessibilityLabel(LS("MLXBarの設定とダウンロード済みランタイムをすべて削除"))
            }
            Section {
                Text(LS("削除完了後にMLXBarは終了します。その後、MLXBar.appをゴミ箱へ移動してください。"))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding()
        .confirmationDialog(LS("MLXBarの全データを削除しますか？"), isPresented: $confirmsRemoval) {
            Button(LS("全データを削除して終了"), role: .destructive) {
                Task { await model.deleteAllAppDataAndQuit() }
            }
            Button(LS("キャンセル"), role: .cancel) {}
        } message: {
            Text(LS("設定、履歴、APIキー、ダウンロード済みランタイム、ログが削除されます。この操作は取り消せません。外部のモデル本体は残ります。"))
        }
    }
}

struct GeneralSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    private var general: [String: Any] { model.settings["general"] as? [String: Any] ?? [:] }
    var body: some View {
        Form {
            Picker("Language", selection: Binding(
                get: { model.guiLanguage },
                set: { value in Task { await model.setGUILanguage(value) } }
            )) {
                Text("English").tag("en")
                Text("日本語").tag("ja")
            }
            Text(LS("English is used by default. You can change the interface language here at any time."))
                .font(.caption).foregroundStyle(.secondary)
            Toggle(LS("GUI終了後もサービスを継続"), isOn: Binding(
                get: { general["continueAfterGUIExit"] as? Bool ?? true },
                set: { value in Task { await model.setConfig("general.continueAfterGUIExit", value: value) } }
            ))
            Toggle(LS("ログイン時に起動"), isOn: Binding(
                get: { general["launchAtLogin"] as? Bool ?? false },
                set: { value in Task { await model.setLaunchAtLogin(value) } }
            ))
            Text(LS("バックグラウンド時は30秒間隔で状態を確認します。")).foregroundStyle(.secondary)
        }.padding()
    }
}

struct ModelSourceSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var isSelectingFolder = false
    @State private var maxTokenLimit = 8192
    @State private var maxQueuedRequests = 16
    @State private var queueTimeoutSeconds = 3600
    @State private var defaultTemperature = 0.7
    @State private var defaultTopP = 1.0
    @State private var defaultRepetitionPenalty = 1.0
    @State private var repetitionContextSize = 20
    @State private var poolEnabled = true
    @State private var maxResidentModels = 2
    @State private var idleTTLSeconds = 900
    @State private var perModelMaxGB = 32
    @State private var totalMemoryPercent = 75
    @State private var systemReserveGB = 4
    @State private var generationConcurrency = 2
    @State private var pinnedModelIds: Set<String> = []
    var roots: [String] { ((model.settings["models"] as? [String: Any])?["roots"] as? [String]) ?? [] }
    var automaticallyLoadsForAPI: Bool { ((model.settings["models"] as? [String: Any])?["autoLoadOnAPIRequest"] as? Bool) ?? true }
    var body: some View {
        // A plain `Form` doesn't reliably scroll on macOS when embedded in a
        // `NavigationSplitView` detail column, so this section's content (the
        // longest on this screen) could get clipped at the bottom of the
        // window with no way to reach it. Wrapping it explicitly guarantees
        // scrolling regardless of window height.
        ScrollView {
        Form {
            Section(LS("APIからのモデル利用")) {
                Toggle(LS("API要求時に必要なモデルを自動ロード"), isOn: Binding(
                    get: { automaticallyLoadsForAPI },
                    set: { value in Task { await model.setConfig("models.autoLoadOnAPIRequest", value: value) } }
                ))
                Text(LS("アプリやモデルWorkerを再起動した後も、OpenAI互換APIで指定されたモデルを自動的に復元します。手動でアンロードした場合は自動ロードしません。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Section(LS("複数モデル常駐")) {
                Toggle(LS("要求されたモデルを別々のWorkerで保持"), isOn: $poolEnabled)
                Stepper("\(LS("最大常駐モデル数")): \(maxResidentModels)",
                        value: $maxResidentModels, in: 1...8)
                Stepper("\(LS("非固定モデルの待機時間")): \(idleTTLSeconds) \(LS("秒"))",
                        value: $idleTTLSeconds, in: 30...86400, step: 30)
                Stepper("\(LS("モデルごとの上限")): \(perModelMaxGB) GB",
                        value: $perModelMaxGB, in: 1...512)
                Stepper("\(LS("全体メモリ上限")): \(totalMemoryPercent)%",
                        value: $totalMemoryPercent, in: 50...90)
                Stepper("\(LS("システム用に残すメモリ")): \(systemReserveGB) GB",
                        value: $systemReserveGB, in: 1...128)
                Stepper("\(LS("同時生成の上限")): \(generationConcurrency)",
                        value: $generationConcurrency, in: 1...8)
                Button(LS("モデル常駐設定を適用")) {
                    Task {
                        await model.setModelPoolSettings(
                            enabled: poolEnabled, maximum: maxResidentModels,
                            ttl: idleTTLSeconds, perModelGB: perModelMaxGB,
                            totalRatio: Double(totalMemoryPercent) / 100,
                            reserveGB: systemReserveGB,
                            generationConcurrency: generationConcurrency)
                    }
                }.buttonStyle(.borderedProminent)
                Text(LS("有効／無効の切り替えと同時生成の上限は次回サービス起動時に反映されます。その他の上限は待機中モデルから安全に反映されます。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                LabeledContent(LS("現在の常駐数"), value: "\(model.residentModelCount)")
                LabeledContent(LS("同時生成"), value: "\(model.activeGenerations) / \(model.generationConcurrency)")
                LabeledContent(LS("予約済みメモリ"), value: ByteCountFormatter.string(
                    fromByteCount: model.modelPoolReservedBytes, countStyle: .memory))
                LabeledContent(LS("常駐メモリ予算"), value: ByteCountFormatter.string(
                    fromByteCount: model.modelPoolBudgetBytes, countStyle: .memory))
                Text(LS("モデルごとに独立したWorkerを使います。異なるモデルは同時生成の上限まで並行して生成し、同一モデルへの要求は到着順に直列化します。1にすると従来どおり全体で1件ずつです。手動ロードや常駐指定したモデルは停止まで保持し、APIが自動ロードしたモデルは未使用時間後に解放します。macOSが深刻なメモリ逼迫を報告した場合は固定モデルも安全のため解放します。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Section(LS("常駐させるモデル")) {
                if model.models.isEmpty {
                    Text(LS("モデルが見つかりません。フォルダを追加して再スキャンしてください。"))
                        .font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                ForEach(model.models.filter { $0.engine != nil }) { item in
                    Toggle(isOn: Binding(
                        get: { pinnedModelIds.contains(item.id) },
                        set: { value in
                            if value { pinnedModelIds.insert(item.id) }
                            else { pinnedModelIds.remove(item.id) }
                            Task { await model.setModelPin(item.id, keepLoaded: value) }
                        }
                    )) {
                        Text(item.name).lineLimit(1).truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                Text(LS("常駐指定したモデルはサービス起動時に自動でロードされ、未使用でも解放されません。今すぐ反映したい場合はモデル画面からロードしてください。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Section(LS("Max token上限")) {
                HStack {
                    TextField(LS("上限"), value: $maxTokenLimit, format: .number)
                        .frame(width: 160)
                    Text("tokens")
                    Button(LS("適用")) { Task { await model.setMaxTokenLimit(maxTokenLimit) } }
                        .buttonStyle(.borderedProminent)
                }
                if let modelLimit = model.loadedModelMaxTokens {
                    tokenLimitRow(LS("ロード中モデルの上限"), "\(MenuBarViewModel.tokenCount(modelLimit)) tokens")
                } else {
                    tokenLimitRow(LS("ロード中モデルの上限"), LS("モデル未ロード／取得できません"))
                }
                tokenLimitRow(LS("現在のAPI有効上限"), "\(MenuBarViewModel.tokenCount(model.effectiveMaxTokens)) tokens")
                Text(LS("API要求がこの値を超えた場合はエラーにせず、この上限へ自動調整します。有効上限は設定値とモデル上限の小さい方です。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Section(LS("既定の生成パラメータ")) {
                parameterRow(LS("温度"), placeholder: "0〜2", value: $defaultTemperature,
                             range: 0...2, step: 0.05, accessibilityLabel: LS("温度"))
                parameterRow("Top P", placeholder: "0〜1", value: $defaultTopP,
                             range: 0...1, step: 0.05, accessibilityLabel: "Top P")
                parameterRow(LS("繰り返しペナルティ"), placeholder: "0.01〜2", value: $defaultRepetitionPenalty,
                             range: 0.01...2, step: 0.05, accessibilityLabel: LS("繰り返しペナルティ"))
                Stepper("\(LS("ペナルティ対象範囲")): \(repetitionContextSize) tokens",
                        value: $repetitionContextSize, in: 1...32768)
                Button(LS("生成パラメータを適用")) {
                    Task {
                        await model.setSamplingDefaults(
                            temperature: defaultTemperature, topP: defaultTopP,
                            repetitionPenalty: defaultRepetitionPenalty,
                            repetitionContextSize: repetitionContextSize)
                    }
                }.buttonStyle(.borderedProminent)
                Text(LS("温度0は決定的な出力、Top Pを小さくすると候補を絞ります。繰り返しペナルティは1.0で無効、1より大きいほど繰り返しを抑えます。API要求が値を指定した場合はAPI側を優先します。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Section(LS("並列リクエスト")) {
                Stepper("\(LS("生成待ち: 最大")) \(maxQueuedRequests)\(LS("件"))", value: $maxQueuedRequests, in: 1...64)
                HStack {
                    TextField(LS("最大待ち時間"), value: $queueTimeoutSeconds, format: .number)
                        .frame(width: 160)
                    Text(LS("秒"))
                    Button(LS("適用")) {
                        Task { await model.setQueueLimits(maximum: maxQueuedRequests,
                                                          timeout: queueTimeoutSeconds) }
                    }.buttonStyle(.borderedProminent)
                }
                Text(LS("ZCodeのsubagentなどから同時に届いた要求を到着順に処理します。待機中も接続を維持し、上限を超えた場合だけエラーを返します。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Section(LS("追加フォルダ")) {
                ForEach(roots, id: \.self) { path in
                    HStack {
                        Text(path).lineLimit(1).truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Button(LS("削除")) { Task { await model.removeModelFolder(path) } }
                    }
                }
                HStack {
                    Button(LS("フォルダを追加…")) { chooseFolder() }
                    Menu(LS("ライブラリから追加…")) {
                        Button(LS("ユーザライブラリ（~/Library）")) { chooseFolder(startingAt: .user) }
                        Button(LS("Macライブラリ（/Library）")) { chooseFolder(startingAt: .system) }
                    }
                }
                .disabled(isSelectingFolder)
                Text(LS("選択画面では隠しフォルダも表示されます。⌘⇧Gで任意のパスを直接入力できます。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Button(LS("今すぐ再スキャン")) { Task { await model.scan() } }
        }
        .padding()
        .task {
            await model.refreshSettings()
            await model.refreshStatus()
            await model.refreshModels()
            maxTokenLimit = model.configuredMaxTokens
            defaultTemperature = model.configuredTemperature
            defaultTopP = model.configuredTopP
            defaultRepetitionPenalty = model.configuredRepetitionPenalty
            repetitionContextSize = model.configuredRepetitionContextSize
            let generation = model.settings["generation"] as? [String: Any] ?? [:]
            maxQueuedRequests = (generation["maxQueuedRequests"] as? NSNumber)?.intValue ?? 16
            queueTimeoutSeconds = (generation["queueTimeoutSeconds"] as? NSNumber)?.intValue ?? 3600
            let pool = ((model.settings["models"] as? [String: Any])?["pool"] as? [String: Any]) ?? [:]
            poolEnabled = pool["enabled"] as? Bool ?? true
            maxResidentModels = (pool["maxResidentModels"] as? NSNumber)?.intValue ?? 2
            idleTTLSeconds = (pool["idleTTLSeconds"] as? NSNumber)?.intValue ?? 900
            perModelMaxGB = (pool["defaultPerModelMaxGB"] as? NSNumber)?.intValue ?? 32
            totalMemoryPercent = Int(((pool["totalMemoryRatio"] as? NSNumber)?.doubleValue ?? 0.75) * 100)
            systemReserveGB = (pool["minimumSystemReserveGB"] as? NSNumber)?.intValue ?? 4
            generationConcurrency = (pool["generationConcurrency"] as? NSNumber)?.intValue ?? 2
            pinnedModelIds = Set((pool["profiles"] as? [[String: Any]] ?? [])
                .filter { ($0["keepLoaded"] as? Bool) ?? false }
                .compactMap { $0["modelId"] as? String })
        }
        }
    }

    /// Label above value, instead of one row with a `Spacer` between them or
    /// `LabeledContent`'s shared alignment column: either can force the label
    /// and value to compete for space on one line and push each other off the
    /// visible edge of the window when the label is long (especially in
    /// English) or the window is narrower than their combined width.
    @ViewBuilder
    private func tokenLimitRow(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.subheadline)
            Text(value).foregroundStyle(.secondary)
        }
    }

    /// Label, text field, and slider stacked on separate lines and width-capped
    /// so this row can't force the whole Form (and window) wider than intended.
    @ViewBuilder
    private func parameterRow(_ label: String, placeholder: String, value: Binding<Double>,
                               range: ClosedRange<Double>, step: Double,
                               accessibilityLabel: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.subheadline)
            HStack {
                TextField(placeholder, value: value, format: .number)
                    .frame(width: 90)
                Slider(value: value, in: range, step: step)
                    .accessibilityLabel(accessibilityLabel)
            }
        }
        .frame(maxWidth: 480)
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

struct APISettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var port = 11435
    @State private var showsToken = false
    @State private var confirmsRegeneration = false
    @State private var confirmsLANEnable = false
    private var api: [String: Any] { model.settings["api"] as? [String: Any] ?? [:] }
    private var security: [String: Any] { model.settings["security"] as? [String: Any] ?? [:] }
    private var allowsLAN: Bool { security["allowLan"] as? Bool ?? false }
    private var allowsRemoteImages: Bool { security["allowRemoteImageUrls"] as? Bool ?? false }
    var body: some View {
        Form {
            Section(LS("APIサーバー")) {
                Toggle(LS("ローカルネットワークへ公開"), isOn: Binding(
                    get: { allowsLAN },
                    set: { enabled in
                        if enabled { confirmsLANEnable = true }
                        else { Task { await model.setLANAccess(false) } }
                    }
                ))
                LabeledContent("Host", value: allowsLAN ? LS("0.0.0.0（LAN）") : LS("127.0.0.1（このMacのみ）"))
                TextField("Port", value: $port, format: .number).frame(width: 120)
                LabeledContent(LS("現在のURL"), value: model.apiURL)
                HStack {
                    Button(LS("URLをコピー")) { model.copyAPIURL() }
                    Button(LS("ポートを適用")) { Task { await model.setPort(port) } }
                }
                if allowsLAN {
                    Label(LS("LAN内の端末からAPIへ接続できます。信頼できるネットワークでのみ使用してください。"),
                          systemImage: "network.badge.shield.half.filled")
                        .foregroundStyle(.orange)
                    if model.lanAPIURLs.isEmpty {
                        Text(LS("LAN用IPを取得できませんでした。Macのネットワーク接続を確認してください。"))
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        ForEach(model.lanAPIURLs, id: \.self) { url in
                            HStack {
                                Text(url).textSelection(.enabled)
                                Spacer()
                                Button(LS("コピー")) { model.copyURL(url) }
                            }
                        }
                    }
                }
            }
            Section(LS("画像入力")) {
                Toggle(LS("外部URLの画像取得を許可"), isOn: Binding(
                    get: { allowsRemoteImages },
                    set: { value in Task { await model.setConfig("security.allowRemoteImageUrls", value: value) } }
                ))
                Text(LS("既定ではAPIの画像はdata URI（base64）のみ受け付けます。有効にすると、MLXBarがhttp(s)の画像URLを代理取得します。プライベートアドレスへの取得は常に拒否します。"))
                    .font(.caption).foregroundStyle(allowsRemoteImages ? .orange : .secondary)
            }
            Section(LS("APIキー")) {
                Toggle(LS("APIキーを要求"), isOn: Binding(
                    get: { api["requireToken"] as? Bool ?? true },
                    set: { value in Task { await model.setConfig("api.requireToken", value: value) } }
                )).disabled(allowsLAN)
                HStack {
                    if showsToken { TextField(LS("APIキー"), text: $model.apiToken).textFieldStyle(.roundedBorder) }
                    else { SecureField(LS("APIキー"), text: $model.apiToken).textFieldStyle(.roundedBorder) }
                    Button { showsToken.toggle() } label: {
                        Image(systemName: showsToken ? "eye.slash" : "eye")
                            .accessibilityLabel(LS(showsToken ? "APIキーを隠す" : "APIキーを表示"))
                    }
                }
                HStack {
                    Button(LS("保存")) { Task { await model.saveAPIToken(model.apiToken) } }.buttonStyle(.borderedProminent)
                    Button(LS("コピー")) { model.copyAPIToken() }.disabled(model.apiToken.isEmpty)
                    Button(LS("再生成…"), role: .destructive) { confirmsRegeneration = true }
                }
                Text(LS(allowsLAN
                        ? "LAN公開中はAPIキーが必須です。別PCでは Authorization: Bearer <APIキー> を指定します。"
                        : (api["requireToken"] as? Bool ?? true)
                        ? "OpenAI互換APIでは Authorization: Bearer <APIキー> を指定します。"
                        : "APIキーなしで利用できます。同じMac上の他のアプリからもアクセス可能になります。"))
                    .font(.caption).foregroundStyle(.secondary)
                if let status = model.secretStatus { Text(status).font(.caption).foregroundStyle(.secondary) }
            }
        }
        .padding()
        .onAppear { port = Int(URL(string: model.apiURL)?.port ?? 11435) }
        .confirmationDialog(LS("APIキーを再生成しますか？"), isPresented: $confirmsRegeneration) {
            Button(LS("再生成"), role: .destructive) { Task { await model.regenerateAPIToken() } }
        } message: {
            Text(LS("現在のAPIキーを使っているクライアントは、設定を更新するまで接続できなくなります。"))
        }
        .confirmationDialog(LS("ローカルネットワークへ公開しますか？"), isPresented: $confirmsLANEnable) {
            Button(LS("APIキー必須で公開"), role: .destructive) {
                Task { await model.setLANAccess(true) }
            }
            Button(LS("キャンセル"), role: .cancel) {}
        } message: {
            Text(LS("同じネットワーク内の端末からMLXBarへ接続可能になります。信頼できるLANでのみ有効にしてください。"))
        }
    }
}

struct LMStudioSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var baseURL = "http://127.0.0.1:1234"
    @State private var showsToken = false
    private var lmStudio: [String: Any] { ((model.settings["models"] as? [String: Any])?["lmStudio"] as? [String: Any]) ?? [:] }
    var body: some View {
        Form {
            TextField("Base URL", text: $baseURL)
            HStack {
                if showsToken { TextField(LS("APIキー（任意）"), text: $model.lmStudioToken) }
                else { SecureField(LS("APIキー（任意）"), text: $model.lmStudioToken) }
                Button { showsToken.toggle() } label: {
                    Image(systemName: showsToken ? "eye.slash" : "eye")
                        .accessibilityLabel(LS(showsToken ? "APIキーを隠す" : "APIキーを表示"))
                }
            }
            Toggle(LS("自動ロード"), isOn: Binding(get: { lmStudio["autoLoad"] as? Bool ?? true }, set: { value in Task { await model.setConfig("models.lmStudio.autoLoad", value: value) } }))
            HStack {
                Button(LS("適用")) {
                    Task {
                        await model.setConfig("models.lmStudio.baseUrl", value: baseURL)
                        await model.saveLMStudioToken(model.lmStudioToken)
                    }
                }.buttonStyle(.borderedProminent)
                Button(LS("APIキーを削除")) {
                    model.lmStudioToken = ""
                    Task { await model.saveLMStudioToken("") }
                }.disabled(model.lmStudioToken.isEmpty)
            }
            if let status = model.secretStatus { Text(status).font(.caption).foregroundStyle(.secondary) }
            Text(LS("LM Studioが停止中でもMLXBarは動作を継続します。GGUFはLM Studio Providerのみに送られます。")).foregroundStyle(.secondary)
        }.padding().onAppear { baseURL = lmStudio["baseUrl"] as? String ?? baseURL }
    }
}
