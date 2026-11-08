import SwiftUI

/// Settings > API Fallback (v5.0.0, DESIGN_v5.0.0.md F-6).
///
/// Layout: one enable switch and three common options, then one card per
/// external provider, each holding its own "fallback model" rows. A row owns
/// everything about one external model: its priority, whether it accepts
/// images, its rate window, its context/output limits, and which MLXBar models
/// it may answer for.
///
/// Every edit is a **partial** write (`updateFallbackModel` with only the
/// changed key), so two fields edited in quick succession cannot overwrite each
/// other with a stale copy of the document.
struct APIFallbackSettingsView: View {
    @ObservedObject var model: MenuBarViewModel

    @State private var enabled = false
    @State private var cooldownSeconds = 60
    @State private var maxAttempts = 3
    @State private var forwardImagesImmediately = true
    @State private var confirmsEnable = false
    @State private var addingProvider = false
    @State private var newProviderName = ""
    @State private var newProviderProtocol = "openai-chat"
    @State private var newProviderURL = ""
    @State private var newProviderTimeout = 120
    @State private var pendingProviderRemoval: FallbackProvider?

    private var settings: FallbackSettings { model.fallbackSettings }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                privacyNotice
                commonOptions
                Divider()
                providersSection
                Divider()
                statusSection
            }
            .padding()
        }
        .task {
            await model.refreshFallback()
            syncFromModel()
            await model.refreshModels()
        }
        .onChange(of: model.fallbackSettings) { _, _ in syncFromModel() }
        .confirmationDialog(LS("APIフォールバックを有効にしますか？"),
                            isPresented: $confirmsEnable, titleVisibility: .visible) {
            Button(LS("有効にする")) {
                Task { await saveCommon(enabled: true) }
            }
            Button(LS("キャンセル"), role: .cancel) {}
        } message: {
            Text(LS("有効にすると、対象のリクエスト内容（プロンプト・画像）が、選択した外部プロバイダーへ送信されます。"))
        }
        .confirmationDialog(LS("このプロバイダーを削除しますか？"),
                            isPresented: Binding(get: { pendingProviderRemoval != nil },
                                                 set: { if !$0 { pendingProviderRemoval = nil } }),
                            titleVisibility: .visible) {
            Button(LS("削除"), role: .destructive) {
                if let provider = pendingProviderRemoval {
                    Task { await model.removeFallbackProvider(provider.id) }
                }
                pendingProviderRemoval = nil
            }
            Button(LS("キャンセル"), role: .cancel) { pendingProviderRemoval = nil }
        } message: {
            Text(LS("このプロバイダーのモデル設定と保存済みのAPIキーも削除されます。"))
        }
    }

    // MARK: - sections

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(LS("APIフォールバック")).font(.title2).bold()
            Text(LS("MLXBarのキューが一杯のときや、ローカルで応答できないときに、登録した外部AIプロバイダーへリクエストを転送します。"))
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var privacyNotice: some View {
        Label {
            Text(LS("有効にすると、対象のリクエスト内容（プロンプト・画像）が、選択した外部プロバイダーへ送信されます。画像は、画像に対応する設定にしたフォールバックモデルにだけ送られます。"))
                .fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: "exclamationmark.shield")
        }
        .font(.caption)
        .foregroundStyle(.orange)
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 8))
    }

    private var commonOptions: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                Toggle(LS("APIフォールバックを有効にする"), isOn: Binding(
                    get: { enabled },
                    set: { newValue in
                        if newValue && !enabled {
                            // Enabling sends prompts off this Mac: confirm first.
                            confirmsEnable = true
                        } else {
                            Task { await saveCommon(enabled: newValue) }
                        }
                    }))
                .toggleStyle(.switch)

                HStack(spacing: 16) {
                    LabeledContent(LS("失敗したモデルを後回しにする秒数")) {
                        TextField("", value: $cooldownSeconds, format: .number)
                            .frame(width: 80)
                            .onSubmit { Task { await saveCommon() } }
                    }
                    LabeledContent(LS("1回の要求で試すモデル数の上限")) {
                        TextField("", value: $maxAttempts, format: .number)
                            .frame(width: 80)
                            .onSubmit { Task { await saveCommon() } }
                    }
                }
                Toggle(LS("画像付きの要求は、キューを待たずに画像対応のフォールバックモデルへ送る"),
                       isOn: Binding(get: { forwardImagesImmediately },
                                     set: { value in
                                         forwardImagesImmediately = value
                                         Task { await saveCommon() }
                                     }))
                Text(LS("優先順位は小さいほど先に試されます。同じ優先順位では、上のプロバイダーから順に使います。"))
                    .font(.caption).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(6)
        }
    }

    private var providersSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(LS("外部プロバイダー")).font(.headline)
                Spacer()
                Button {
                    addingProvider.toggle()
                } label: {
                    Label(LS("プロバイダーを追加"), systemImage: "plus")
                }
            }
            if addingProvider {
                addProviderForm
            }
            if settings.providers.isEmpty {
                ContentUnavailableView(LS("まだプロバイダーが登録されていません"),
                                       systemImage: "network",
                                       description: Text(LS("OpenAI・Anthropicなど、OpenAI互換またはAnthropic互換のAPIを登録できます。")))
            } else {
                ForEach(settings.providers) { provider in
                    FallbackProviderCard(model: model, provider: provider,
                                         onRemove: { pendingProviderRemoval = provider })
                }
            }
        }
    }

    private var addProviderForm: some View {
        GroupBox(LS("新しいプロバイダー")) {
            VStack(alignment: .leading, spacing: 8) {
                TextField(LS("表示名（例: OpenAI）"), text: $newProviderName)
                Picker(LS("プロトコル"), selection: $newProviderProtocol) {
                    ForEach(FallbackProtocol.all, id: \.self) { name in
                        Text(LS(FallbackProtocol.labelKey(name))).tag(name)
                    }
                }
                TextField(LS("ベースURL（例: https://api.openai.com/v1）"), text: $newProviderURL)
                LabeledContent(LS("タイムアウト（秒）")) {
                    TextField("", value: $newProviderTimeout, format: .number).frame(width: 90)
                }
                HStack {
                    Spacer()
                    Button(LS("キャンセル")) {
                        addingProvider = false
                        resetProviderForm()
                    }
                    Button(LS("追加")) {
                        Task {
                            await model.addFallbackProvider(
                                name: newProviderName, protocolName: newProviderProtocol,
                                baseURL: newProviderURL, timeoutSeconds: newProviderTimeout)
                            addingProvider = false
                            resetProviderForm()
                        }
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(newProviderName.trimmingCharacters(in: .whitespaces).isEmpty
                              || newProviderURL.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(6)
        }
    }

    private var statusSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(LS("状態")).font(.headline)
                Spacer()
                Button(LS("更新")) { Task { await model.refreshFallbackStatus() } }
                Button(LS("レートをリセット")) { Task { await model.resetFallbackLimits() } }
            }
            if let outcome = model.fallbackLastOutcome {
                LabeledContent(LS("直近のフォールバック")) {
                    Text(outcomeText(outcome))
                }
            } else {
                Text(LS("まだフォールバックは実行されていません。"))
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let message = model.fallbackMessage {
                Text(message).font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    // MARK: - helpers

    private func outcomeText(_ outcome: FallbackOutcome) -> String {
        let reason: String
        switch outcome.reason {
        case "QUEUE_FULL": reason = LS("キューが一杯")
        case "QUEUE_TIMEOUT": reason = LS("待ち時間の上限超過")
        case "ENGINE_BUSY": reason = LS("別のモデルが応答中")
        case "images": reason = LS("画像付きの要求")
        case .some(let other): reason = other
        case .none: reason = ""
        }
        let destination = [outcome.provider, outcome.model].compactMap { $0 }.joined(separator: " / ")
        if outcome.failed {
            return LS("すべての送信先が失敗しました") + (reason.isEmpty ? "" : "（\(reason)）")
        }
        return destination.isEmpty ? reason : "\(destination)（\(reason)）"
    }

    private func syncFromModel() {
        let current = model.fallbackSettings
        enabled = current.enabled
        cooldownSeconds = current.cooldownSeconds
        maxAttempts = current.maxAttempts
        forwardImagesImmediately = current.forwardImagesImmediately
    }

    private func saveCommon(enabled: Bool? = nil) async {
        let target = enabled ?? self.enabled
        self.enabled = target
        await model.setFallbackCommon(enabled: target, cooldownSeconds: cooldownSeconds,
                                      maxAttempts: maxAttempts,
                                      forwardImagesImmediately: forwardImagesImmediately)
    }

    private func resetProviderForm() {
        newProviderName = ""
        newProviderProtocol = "openai-chat"
        newProviderURL = ""
        newProviderTimeout = 120
    }
}

/// One provider card: identity, key, connection test, and its model rows.
private struct FallbackProviderCard: View {
    @ObservedObject var model: MenuBarViewModel
    let provider: FallbackProvider
    let onRemove: () -> Void

    @State private var name = ""
    @State private var baseURL = ""
    @State private var protocolName = "openai-chat"
    @State private var timeoutSeconds = 120
    @State private var apiKey = ""
    @State private var confirmsKeyRemoval = false
    @State private var addingModel = false
    @State private var newModelName = ""
    @State private var newModelPriority = 10
    @State private var newModelImages = false
    @State private var newModelAllSources = true

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Toggle("", isOn: Binding(
                        get: { provider.enabled },
                        set: { value in
                            Task { await model.updateFallbackProvider(provider.id, name: nil,
                                                                      protocolName: nil, baseURL: nil,
                                                                      timeoutSeconds: nil, enabled: value) }
                        }))
                    .toggleStyle(.switch)
                    .labelsHidden()
                    TextField(LS("表示名"), text: $name)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { saveIdentity() }
                    Spacer()
                    Button(role: .destructive, action: onRemove) {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.borderless)
                    .help(LS("このプロバイダーを削除"))
                }
                HStack(spacing: 8) {
                    Picker(LS("プロトコル"), selection: $protocolName) {
                        ForEach(FallbackProtocol.all, id: \.self) { item in
                            Text(LS(FallbackProtocol.labelKey(item))).tag(item)
                        }
                    }
                    .onChange(of: protocolName) { _, _ in saveIdentity() }
                    .frame(maxWidth: 260)
                    LabeledContent(LS("タイムアウト（秒）")) {
                        TextField("", value: $timeoutSeconds, format: .number)
                            .frame(width: 70)
                            .onSubmit { saveIdentity() }
                    }
                }
                TextField(LS("ベースURL"), text: $baseURL)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { saveIdentity() }
                HStack {
                    SecureField(provider.apiKeyConfigured ? LS("APIキー（保存済み。変更する場合だけ入力）")
                                                          : LS("APIキー"),
                                text: $apiKey)
                        .textFieldStyle(.roundedBorder)
                    Button(LS("保存")) {
                        Task {
                            await model.setFallbackApiKey(provider.id, key: apiKey)
                            apiKey = ""
                        }
                    }
                    .disabled(apiKey.isEmpty)
                    if provider.apiKeyConfigured {
                        Button(LS("削除"), role: .destructive) { confirmsKeyRemoval = true }
                    }
                    Button {
                        Task { await model.testFallbackProvider(provider.id) }
                    } label: {
                        if model.fallbackTesting.contains(provider.id) {
                            ProgressView().controlSize(.small)
                        } else {
                            Text(LS("接続テスト"))
                        }
                    }
                    .disabled(model.fallbackTesting.contains(provider.id) || provider.models.isEmpty)
                }
                if !provider.apiKeyConfigured {
                    Text(LS("APIキーが未設定です。キーが必要なプロバイダーでは接続できません。"))
                        .font(.caption).foregroundStyle(.orange)
                }

                Divider()
                HStack {
                    Text(LS("フォールバックモデル")).font(.subheadline).bold()
                    Spacer()
                    Button {
                        addingModel.toggle()
                    } label: {
                        Label(LS("モデルを追加"), systemImage: "plus")
                    }
                }
                if addingModel {
                    addModelForm
                }
                if provider.models.isEmpty {
                    Text(LS("モデルが登録されていません。"))
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    ForEach(provider.models) { item in
                        FallbackModelRow(model: model, provider: provider, item: item)
                        if item.id != provider.models.last?.id { Divider() }
                    }
                }
            }
            .padding(6)
        }
        .onAppear(perform: sync)
        .onChange(of: provider) { _, _ in sync() }
        .confirmationDialog(LS("APIキーを削除しますか？"), isPresented: $confirmsKeyRemoval) {
            Button(LS("削除"), role: .destructive) {
                Task { await model.setFallbackApiKey(provider.id, key: "") }
            }
            Button(LS("キャンセル"), role: .cancel) {}
        } message: {
            Text(LS("保存されているAPIキーを削除します。"))
        }
    }

    private var addModelForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                TextField(LS("モデル名（例: gpt-4.1-mini）"), text: $newModelName)
                    .textFieldStyle(.roundedBorder)
                LabeledContent(LS("優先順位")) {
                    TextField("", value: $newModelPriority, format: .number).frame(width: 70)
                }
            }
            HStack {
                Toggle(LS("画像に対応"), isOn: $newModelImages)
                Toggle(LS("すべてのMLXBarモデルをフォールバック元にする"), isOn: $newModelAllSources)
            }
            HStack {
                Spacer()
                Button(LS("キャンセル")) { addingModel = false }
                Button(LS("追加")) {
                    Task {
                        await model.addFallbackModel(
                            provider.id, model: newModelName, priority: newModelPriority,
                            supportsImages: newModelImages,
                            sourceModels: newModelAllSources ? [FallbackModel.wildcard] : [],
                            sourceModelsExplicit: !newModelAllSources)
                        addingModel = false
                        newModelName = ""
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(newModelName.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
    }

    private func sync() {
        name = provider.name
        baseURL = provider.baseURL
        protocolName = provider.protocolName
        timeoutSeconds = provider.timeoutSeconds
    }

    private func saveIdentity() {
        Task {
            await model.updateFallbackProvider(provider.id, name: name, protocolName: protocolName,
                                               baseURL: baseURL, timeoutSeconds: timeoutSeconds,
                                               enabled: nil)
        }
    }
}

/// One external model row: limits, capabilities and source-model assignment.
private struct FallbackModelRow: View {
    @ObservedObject var model: MenuBarViewModel
    let provider: FallbackProvider
    let item: FallbackModel

    @State private var priority = 10
    @State private var maxRequests = 0
    @State private var rateWindow = 60
    @State private var maxOutputTokens = 0
    @State private var maxContextTokens = 0
    @State private var imageDetail = "auto"
    @State private var modelName = ""
    @State private var label = ""
    @State private var temperature = ""
    @State private var topP = ""
    @State private var extraHeaders = ""
    @State private var showsAdvanced = false
    @State private var showsSources = false

    private var usage: FallbackUsage? {
        model.fallbackUsage.first { $0.providerId == provider.id && $0.modelId == item.id }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Toggle("", isOn: Binding(
                    get: { item.enabled },
                    set: { value in
                        Task { await model.updateFallbackModel(provider.id, modelId: item.id,
                                                               patch: ["enabled": value]) }
                    }))
                .toggleStyle(.switch)
                .labelsHidden()
                Text(item.displayName).font(.subheadline).bold()
                if item.supportsImages {
                    Label(LS("画像対応"), systemImage: "photo")
                        .font(.caption2).foregroundStyle(.blue)
                }
                if item.coversAllModels {
                    Label(LS("すべてのモデル"), systemImage: "asterisk")
                        .font(.caption2).foregroundStyle(.secondary)
                } else if item.explicitSources.isEmpty {
                    Label(LS("フォールバック元が未選択"), systemImage: "exclamationmark.triangle")
                        .font(.caption2).foregroundStyle(.orange)
                }
                Spacer()
                if let usage, usage.coolingDown {
                    Text(LS("クールダウン中")).font(.caption2).foregroundStyle(.orange)
                }
                if let usage, item.maxRequests > 0 {
                    Text("\(usage.usedInWindow)/\(item.maxRequests)")
                        .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                }
                Button(role: .destructive) {
                    Task { await model.removeFallbackModel(provider.id, modelId: item.id) }
                } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .help(LS("このモデルを削除"))
            }

            HStack(spacing: 8) {
                TextField(LS("モデル名（例: gpt-4.1-mini）"), text: $modelName)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { saveIdentity() }
                TextField(LS("表示名"), text: $label)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 180)
                    .onSubmit { saveIdentity() }
            }
            .font(.caption)
            HStack(spacing: 16) {
                LabeledContent(LS("優先順位")) {
                    TextField("", value: $priority, format: .number)
                        .frame(width: 64)
                        .onSubmit { save(["priority": priority]) }
                }
                Toggle(LS("画像に対応"), isOn: Binding(
                    get: { item.supportsImages },
                    set: { value in
                        Task { await model.updateFallbackModel(provider.id, modelId: item.id,
                                                               patch: ["supportsImages": value]) }
                    }))
                .toggleStyle(.checkbox)
                Button(LS("フォールバック元を選択…")) { showsSources.toggle() }
                    .buttonStyle(.link)
                Button(LS("詳細")) { showsAdvanced.toggle() }
                    .buttonStyle(.link)
            }
            .font(.caption)

            if showsSources {
                sourceSelector
            }
            if showsAdvanced {
                advancedOptions
            }
        }
        .padding(.vertical, 4)
        .onAppear(perform: sync)
        .onChange(of: item) { _, _ in sync() }
    }

    private var sourceSelector: some View {
        VStack(alignment: .leading, spacing: 6) {
            Toggle(LS("すべてのMLXBarモデルをフォールバック元にする"), isOn: Binding(
                get: { item.coversAllModels },
                set: { value in
                    var sources = item.explicitSources
                    if value { sources.insert(FallbackModel.wildcard, at: 0) }
                    save(["sourceModels": sources, "sourceModelsExplicit": !value])
                }))
            .toggleStyle(.checkbox)
            Text(LS("個別のモデルを選ぶと、そのモデルは「すべてのモデル」より優先されます。"))
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if model.models.isEmpty {
                Text(LS("MLXBarのモデル一覧が空です。モデルをスキャンしてください。"))
                    .font(.caption2).foregroundStyle(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(model.models) { catalog in
                            Toggle(isOn: Binding(
                                get: { item.isAssigned(to: catalog.id) },
                                set: { checked in
                                    var sources = item.explicitSources
                                    if checked {
                                        if !sources.contains(where: {
                                            $0.caseInsensitiveCompare(catalog.id) == .orderedSame
                                        }) {
                                            sources.append(catalog.id)
                                        }
                                    } else {
                                        sources.removeAll {
                                            $0.caseInsensitiveCompare(catalog.id) == .orderedSame
                                        }
                                    }
                                    if item.coversAllModels { sources.insert(FallbackModel.wildcard, at: 0) }
                                    save(["sourceModels": sources,
                                          "sourceModelsExplicit": !item.coversAllModels])
                                })) {
                                Text(catalog.name).font(.caption)
                            }
                            .toggleStyle(.checkbox)
                        }
                    }
                }
                .frame(maxHeight: 180)
            }
        }
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
    }

    private var advancedOptions: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 16) {
                LabeledContent(LS("レート上限（0で無制限）")) {
                    TextField("", value: $maxRequests, format: .number)
                        .frame(width: 70)
                        .onSubmit { save(["maxRequests": maxRequests]) }
                }
                LabeledContent(LS("レートの集計秒数")) {
                    TextField("", value: $rateWindow, format: .number)
                        .frame(width: 70)
                        .onSubmit { save(["rateWindowSeconds": rateWindow]) }
                }
            }
            HStack(spacing: 16) {
                LabeledContent(LS("最大出力トークン（0で要求値）")) {
                    TextField("", value: $maxOutputTokens, format: .number)
                        .frame(width: 80)
                        .onSubmit { save(["maxOutputTokens": maxOutputTokens]) }
                }
                LabeledContent(LS("最大コンテキスト（0で無制限）")) {
                    TextField("", value: $maxContextTokens, format: .number)
                        .frame(width: 90)
                        .onSubmit { save(["maxContextTokens": maxContextTokens]) }
                }
            }
            Picker(LS("画像の詳細度"), selection: $imageDetail) {
                Text(LS("自動")).tag("auto")
                Text(LS("低（高速・低コスト）")).tag("low")
                Text(LS("高（詳細）")).tag("high")
            }
            .onChange(of: imageDetail) { _, value in save(["imageDetail": value]) }
            .frame(maxWidth: 320)
            HStack(spacing: 16) {
                LabeledContent(LS("温度（空欄で要求値）")) {
                    TextField("", text: $temperature)
                        .frame(width: 70)
                        .onSubmit { saveSampling() }
                }
                LabeledContent(LS("top_p（空欄で要求値）")) {
                    TextField("", text: $topP)
                        .frame(width: 70)
                        .onSubmit { saveSampling() }
                }
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(LS("追加HTTPヘッダー（1行に1つ、名前: 値）"))
                TextEditor(text: $extraHeaders)
                    .font(.system(.caption, design: .monospaced))
                    .frame(height: 56)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
                HStack {
                    Spacer()
                    Button(LS("ヘッダーを保存")) { saveHeaders() }
                }
            }
            Text(LS("最大コンテキストを超える会話は、古いメッセージから自動的に省いて送信します。システムメッセージは残します。"))
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.caption)
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
    }

    private func sync() {
        priority = item.priority
        maxRequests = item.maxRequests
        rateWindow = item.rateWindowSeconds
        maxOutputTokens = item.maxOutputTokens
        maxContextTokens = item.maxContextTokens
        imageDetail = item.imageDetail
        modelName = item.model
        label = item.label
        temperature = item.temperature.map { String(format: "%g", $0) } ?? ""
        topP = item.topP.map { String(format: "%g", $0) } ?? ""
        extraHeaders = item.extraHeaders
            .sorted { $0.key < $1.key }
            .map { "\($0.key): \($0.value)" }
            .joined(separator: "\n")
    }

    /// Model name / display label. Refused (with a message) when the name is
    /// empty, because the coordinator rejects an empty `model`.
    private func saveIdentity() {
        let trimmed = modelName.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            model.errorMessage = LS("モデル名を入力してください")
            modelName = item.model
            return
        }
        save(["model": trimmed, "label": label.trimmingCharacters(in: .whitespaces)])
    }

    /// Temperature / top_p. An empty field means "use the request's value",
    /// which the coordinator expresses as null.
    private func saveSampling() {
        var patch: [String: Any] = [:]
        if let value = parseOptionalNumber(temperature, range: 0...2) {
            patch["temperature"] = value ?? NSNull()
        } else {
            model.errorMessage = LS("温度は0〜2の数値で指定してください")
            temperature = item.temperature.map { String(format: "%g", $0) } ?? ""
            return
        }
        if let value = parseOptionalNumber(topP, range: 0.0001...1) {
            patch["topP"] = value ?? NSNull()
        } else {
            model.errorMessage = LS("top_pは0より大きく1以下の数値で指定してください")
            topP = item.topP.map { String(format: "%g", $0) } ?? ""
            return
        }
        save(patch)
    }

    /// Extra headers, one `Name: value` per line. Rejects a line without a
    /// colon instead of silently dropping it.
    private func saveHeaders() {
        var headers: [String: String] = [:]
        for line in extraHeaders.split(separator: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty { continue }
            guard let separator = trimmed.firstIndex(of: ":") else {
                model.errorMessage = LS("ヘッダーは「名前: 値」の形式で1行に1つ入力してください")
                return
            }
            let name = String(trimmed[..<separator]).trimmingCharacters(in: .whitespaces)
            let value = String(trimmed[trimmed.index(after: separator)...])
                .trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else {
                model.errorMessage = LS("ヘッダーは「名前: 値」の形式で1行に1つ入力してください")
                return
            }
            headers[name] = value
        }
        save(["extraHeaders": headers])
    }

    private func parseOptionalNumber(_ text: String, range: ClosedRange<Double>) -> Double?? {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return .some(nil) }
        guard let value = Double(trimmed), range.contains(value) else { return nil }
        return .some(value)
    }

    private func save(_ patch: [String: Any]) {
        Task { await model.updateFallbackModel(provider.id, modelId: item.id, patch: patch) }
    }
}
