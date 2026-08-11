import SwiftUI

struct MLXBarSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var selection = "APIサーバー"
    private let pages = ["一般", "モデル", "APIサーバー", "LM Studio", "ランタイム", "詳細", "削除"]

    var body: some View {
        NavigationSplitView {
            List(pages, id: \.self, selection: $selection) { page in
                Text(LocalizedStringKey(page))
            }
        } detail: {
            switch selection {
            case "一般": GeneralSettingsView(model: model)
            case "モデル": ModelSourceSettingsView(model: model)
            case "APIサーバー": APISettingsView(model: model)
            case "LM Studio": LMStudioSettingsView(model: model)
            case "ランタイム": RuntimeManagerView(model: model)
            case "詳細": DiagnosticsSettingsView(model: model)
            case "削除": DataRemovalSettingsView(model: model)
            default: EmptyView()
            }
        }.task { await model.refreshSettings(); await model.refreshSecrets() }
    }
}

struct DiagnosticsSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var confirmsClear = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("最近のAPIログ").font(.headline)
                Spacer()
                Button("更新") { Task { await model.refreshRecentLogs() } }
                Button("コピー") { model.copyRecentLogs() }.disabled(model.recentLogs.isEmpty)
                Button("消去…", role: .destructive) { confirmsClear = true }.disabled(model.recentLogs.isEmpty)
            }
            Text("リクエスト本文、応答本文、APIキーは記録しません。最新2,000件だけを保存し、この画面には500件まで表示します。")
                .font(.caption).foregroundStyle(.secondary)
            if let status = model.logStatus { Text(status).font(.caption).foregroundStyle(.secondary) }
            if model.recentLogs.isEmpty {
                ContentUnavailableView("ログはありません", systemImage: "doc.text.magnifyingglass")
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
        .confirmationDialog("APIログを消去しますか？", isPresented: $confirmsClear) {
            Button("ログを消去", role: .destructive) { Task { await model.clearRecentLogs() } }
            Button("キャンセル", role: .cancel) {}
        } message: { Text("この操作は取り消せません。モデルや設定には影響しません。") }
    }
}

struct DataRemovalSettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var confirmsRemoval = false

    var body: some View {
        Form {
            Section("MLXBarのデータを削除") {
                Text("設定、履歴、APIキー、MLXBarがダウンロードしたランタイム、ログをこのMacから削除します。")
                Text("Hugging FaceやLM Studioなど、外部のモデルフォルダにあるモデル本体は削除しません。")
                    .font(.caption).foregroundStyle(.secondary)
                if model.isRemovingAllData {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text("サービスを停止してデータを削除中…")
                    }
                }
                Button("すべてのデータを削除して終了…", role: .destructive) {
                    confirmsRemoval = true
                }
                .disabled(model.isRemovingAllData)
                .accessibilityLabel("MLXBarの設定とダウンロード済みランタイムをすべて削除")
            }
            Section {
                Text("削除完了後にMLXBarは終了します。その後、MLXBar.appをゴミ箱へ移動してください。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding()
        .confirmationDialog("MLXBarの全データを削除しますか？", isPresented: $confirmsRemoval) {
            Button("全データを削除して終了", role: .destructive) {
                Task { await model.deleteAllAppDataAndQuit() }
            }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("設定、履歴、APIキー、ダウンロード済みランタイム、ログが削除されます。この操作は取り消せません。外部のモデル本体は残ります。")
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
            Text("English is used by default. You can change the interface language here at any time.")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("GUI終了後もサービスを継続", isOn: Binding(
                get: { general["continueAfterGUIExit"] as? Bool ?? true },
                set: { value in Task { await model.setConfig("general.continueAfterGUIExit", value: value) } }
            ))
            Toggle("ログイン時に起動", isOn: Binding(
                get: { general["launchAtLogin"] as? Bool ?? false },
                set: { value in Task { await model.setLaunchAtLogin(value) } }
            ))
            Text("バックグラウンド時は30秒間隔で状態を確認します。").foregroundStyle(.secondary)
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
    var roots: [String] { ((model.settings["models"] as? [String: Any])?["roots"] as? [String]) ?? [] }
    var automaticallyLoadsForAPI: Bool { ((model.settings["models"] as? [String: Any])?["autoLoadOnAPIRequest"] as? Bool) ?? true }
    var body: some View {
        Form {
            Section("APIからのモデル利用") {
                Toggle("API要求時に必要なモデルを自動ロード", isOn: Binding(
                    get: { automaticallyLoadsForAPI },
                    set: { value in Task { await model.setConfig("models.autoLoadOnAPIRequest", value: value) } }
                ))
                Text("アプリやモデルWorkerを再起動した後も、OpenAI互換APIで指定されたモデルを自動的に復元します。手動でアンロードした場合は自動ロードしません。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Max token上限") {
                HStack {
                    TextField("上限", value: $maxTokenLimit, format: .number)
                        .frame(width: 160)
                    Text("tokens")
                    Button("適用") { Task { await model.setMaxTokenLimit(maxTokenLimit) } }
                        .buttonStyle(.borderedProminent)
                }
                if let modelLimit = model.loadedModelMaxTokens {
                    LabeledContent("ロード中モデルの上限",
                                   value: "\(MenuBarViewModel.tokenCount(modelLimit)) tokens")
                } else {
                    LabeledContent("ロード中モデルの上限", value: "モデル未ロード／取得できません")
                }
                LabeledContent("現在のAPI有効上限",
                               value: "\(MenuBarViewModel.tokenCount(model.effectiveMaxTokens)) tokens")
                Text("API要求がこの値を超えた場合はエラーにせず、この上限へ自動調整します。有効上限は設定値とモデル上限の小さい方です。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("既定の生成パラメータ") {
                LabeledContent("温度") {
                    TextField("0〜2", value: $defaultTemperature, format: .number)
                        .frame(width: 110)
                }
                Slider(value: $defaultTemperature, in: 0...2, step: 0.05)
                LabeledContent("Top P") {
                    TextField("0〜1", value: $defaultTopP, format: .number)
                        .frame(width: 110)
                }
                Slider(value: $defaultTopP, in: 0...1, step: 0.05)
                LabeledContent("繰り返しペナルティ") {
                    TextField("0.01〜2", value: $defaultRepetitionPenalty, format: .number)
                        .frame(width: 110)
                }
                Slider(value: $defaultRepetitionPenalty, in: 0.01...2, step: 0.05)
                Stepper("ペナルティ対象範囲: \(repetitionContextSize) tokens",
                        value: $repetitionContextSize, in: 1...32768)
                Button("生成パラメータを適用") {
                    Task {
                        await model.setSamplingDefaults(
                            temperature: defaultTemperature, topP: defaultTopP,
                            repetitionPenalty: defaultRepetitionPenalty,
                            repetitionContextSize: repetitionContextSize)
                    }
                }.buttonStyle(.borderedProminent)
                Text("温度0は決定的な出力、Top Pを小さくすると候補を絞ります。繰り返しペナルティは1.0で無効、1より大きいほど繰り返しを抑えます。API要求が値を指定した場合はAPI側を優先します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("並列リクエスト") {
                Stepper("生成待ち: 最大 \(maxQueuedRequests)件", value: $maxQueuedRequests, in: 1...64)
                HStack {
                    TextField("最大待ち時間", value: $queueTimeoutSeconds, format: .number)
                        .frame(width: 160)
                    Text("秒")
                    Button("適用") {
                        Task { await model.setQueueLimits(maximum: maxQueuedRequests,
                                                          timeout: queueTimeoutSeconds) }
                    }.buttonStyle(.borderedProminent)
                }
                Text("ZCodeのsubagentなどから同時に届いた要求を到着順に処理します。待機中も接続を維持し、上限を超えた場合だけエラーを返します。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("追加フォルダ") {
                ForEach(roots, id: \.self) { path in
                    HStack {
                        Text(path).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Button("削除") { Task { await model.removeModelFolder(path) } }
                    }
                }
                HStack {
                    Button("フォルダを追加…") { chooseFolder() }
                    Menu("ライブラリから追加…") {
                        Button("ユーザライブラリ（~/Library）") { chooseFolder(startingAt: .user) }
                        Button("Macライブラリ（/Library）") { chooseFolder(startingAt: .system) }
                    }
                }
                .disabled(isSelectingFolder)
                Text("選択画面では隠しフォルダも表示されます。⌘⇧Gで任意のパスを直接入力できます。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("今すぐ再スキャン") { Task { await model.scan() } }
        }
        .padding()
        .task {
            await model.refreshSettings()
            await model.refreshStatus()
            maxTokenLimit = model.configuredMaxTokens
            defaultTemperature = model.configuredTemperature
            defaultTopP = model.configuredTopP
            defaultRepetitionPenalty = model.configuredRepetitionPenalty
            repetitionContextSize = model.configuredRepetitionContextSize
            let generation = model.settings["generation"] as? [String: Any] ?? [:]
            maxQueuedRequests = (generation["maxQueuedRequests"] as? NSNumber)?.intValue ?? 16
            queueTimeoutSeconds = (generation["queueTimeoutSeconds"] as? NSNumber)?.intValue ?? 3600
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

struct APISettingsView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var port = 11435
    @State private var showsToken = false
    @State private var confirmsRegeneration = false
    @State private var confirmsLANEnable = false
    private var api: [String: Any] { model.settings["api"] as? [String: Any] ?? [:] }
    private var security: [String: Any] { model.settings["security"] as? [String: Any] ?? [:] }
    private var allowsLAN: Bool { security["allowLan"] as? Bool ?? false }
    var body: some View {
        Form {
            Section("APIサーバー") {
                Toggle("ローカルネットワークへ公開", isOn: Binding(
                    get: { allowsLAN },
                    set: { enabled in
                        if enabled { confirmsLANEnable = true }
                        else { Task { await model.setLANAccess(false) } }
                    }
                ))
                LabeledContent("Host", value: allowsLAN ? "0.0.0.0（LAN）" : "127.0.0.1（このMacのみ）")
                TextField("Port", value: $port, format: .number).frame(width: 120)
                LabeledContent("現在のURL", value: model.apiURL)
                HStack {
                    Button("URLをコピー") { model.copyAPIURL() }
                    Button("ポートを適用") { Task { await model.setPort(port) } }
                }
                if allowsLAN {
                    Label("LAN内の端末からAPIへ接続できます。信頼できるネットワークでのみ使用してください。",
                          systemImage: "network.badge.shield.half.filled")
                        .foregroundStyle(.orange)
                    if model.lanAPIURLs.isEmpty {
                        Text("LAN用IPを取得できませんでした。Macのネットワーク接続を確認してください。")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        ForEach(model.lanAPIURLs, id: \.self) { url in
                            HStack {
                                Text(url).textSelection(.enabled)
                                Spacer()
                                Button("コピー") { model.copyURL(url) }
                            }
                        }
                    }
                }
            }
            Section("APIキー") {
                Toggle("APIキーを要求", isOn: Binding(
                    get: { api["requireToken"] as? Bool ?? true },
                    set: { value in Task { await model.setConfig("api.requireToken", value: value) } }
                )).disabled(allowsLAN)
                HStack {
                    if showsToken { TextField("APIキー", text: $model.apiToken).textFieldStyle(.roundedBorder) }
                    else { SecureField("APIキー", text: $model.apiToken).textFieldStyle(.roundedBorder) }
                    Button { showsToken.toggle() } label: {
                        Image(systemName: showsToken ? "eye.slash" : "eye")
                            .accessibilityLabel(showsToken ? "APIキーを隠す" : "APIキーを表示")
                    }
                }
                HStack {
                    Button("保存") { Task { await model.saveAPIToken(model.apiToken) } }.buttonStyle(.borderedProminent)
                    Button("コピー") { model.copyAPIToken() }.disabled(model.apiToken.isEmpty)
                    Button("再生成…", role: .destructive) { confirmsRegeneration = true }
                }
                Text(allowsLAN
                     ? "LAN公開中はAPIキーが必須です。別PCでは Authorization: Bearer <APIキー> を指定します。"
                     : (api["requireToken"] as? Bool ?? true)
                     ? "OpenAI互換APIでは Authorization: Bearer <APIキー> を指定します。"
                     : "APIキーなしで利用できます。同じMac上の他のアプリからもアクセス可能になります。")
                    .font(.caption).foregroundStyle(.secondary)
                if let status = model.secretStatus { Text(status).font(.caption).foregroundStyle(.secondary) }
            }
        }
        .padding()
        .onAppear { port = Int(URL(string: model.apiURL)?.port ?? 11435) }
        .confirmationDialog("APIキーを再生成しますか？", isPresented: $confirmsRegeneration) {
            Button("再生成", role: .destructive) { Task { await model.regenerateAPIToken() } }
        } message: {
            Text("現在のAPIキーを使っているクライアントは、設定を更新するまで接続できなくなります。")
        }
        .confirmationDialog("ローカルネットワークへ公開しますか？", isPresented: $confirmsLANEnable) {
            Button("APIキー必須で公開", role: .destructive) {
                Task { await model.setLANAccess(true) }
            }
            Button("キャンセル", role: .cancel) {}
        } message: {
            Text("同じネットワーク内の端末からMLXBarへ接続可能になります。信頼できるLANでのみ有効にしてください。")
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
                if showsToken { TextField("APIキー（任意）", text: $model.lmStudioToken) }
                else { SecureField("APIキー（任意）", text: $model.lmStudioToken) }
                Button { showsToken.toggle() } label: {
                    Image(systemName: showsToken ? "eye.slash" : "eye")
                        .accessibilityLabel(showsToken ? "APIキーを隠す" : "APIキーを表示")
                }
            }
            Toggle("自動ロード", isOn: Binding(get: { lmStudio["autoLoad"] as? Bool ?? true }, set: { value in Task { await model.setConfig("models.lmStudio.autoLoad", value: value) } }))
            HStack {
                Button("適用") {
                    Task {
                        await model.setConfig("models.lmStudio.baseUrl", value: baseURL)
                        await model.saveLMStudioToken(model.lmStudioToken)
                    }
                }.buttonStyle(.borderedProminent)
                Button("APIキーを削除") {
                    model.lmStudioToken = ""
                    Task { await model.saveLMStudioToken("") }
                }.disabled(model.lmStudioToken.isEmpty)
            }
            if let status = model.secretStatus { Text(status).font(.caption).foregroundStyle(.secondary) }
            Text("LM Studioが停止中でもMLXBarは動作を継続します。GGUFはLM Studio Providerのみに送られます。").foregroundStyle(.secondary)
        }.padding().onAppear { baseURL = lmStudio["baseUrl"] as? String ?? baseURL }
    }
}
