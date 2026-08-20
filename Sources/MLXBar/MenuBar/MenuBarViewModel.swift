import AppKit
import Foundation
import ServiceManagement
import SwiftUI

struct CatalogModel: Identifiable, Hashable {
    let id: String
    let name: String
    let source: String
    let format: String
    let engine: String?
    let reason: String
    let size: Int64
    let modalities: [String]

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, let name = value["name"] as? String else { return nil }
        self.id = id; self.name = name
        source = value["source"] as? String ?? "unknown"
        format = value["format"] as? String ?? "unknown"
        engine = value["engine"] as? String
        reason = value["reason"] as? String ?? ""
        size = (value["size_bytes"] as? NSNumber)?.int64Value ?? 0
        modalities = value["modalities"] as? [String] ?? []
    }
}

struct RuntimeInfo: Identifiable {
    let id: String
    let active: String?
    let slots: [[String: Any]]
    let history: [[String: Any]]
    let activeJob: [String: Any]?

    var activeVersion: String? {
        guard let active,
              let slot = slots.first(where: { $0["id"] as? String == active }) else { return nil }
        return (slot["probe"] as? [String: Any])?["version"] as? String
    }
}

struct RuntimeUpdateInfo {
    let currentVersion: String?
    let candidateVersion: String?
    let updateAvailable: Bool
    let releaseURL: String?
    let checkedAt: Date?
    let versionStatus: String

    init(_ value: [String: Any]) {
        currentVersion = value["currentVersion"] as? String
        candidateVersion = value["candidateVersion"] as? String
        updateAvailable = value["updateAvailable"] as? Bool ?? false
        releaseURL = value["releaseUrl"] as? String
        checkedAt = (value["checkedAt"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
        versionStatus = value["versionStatus"] as? String ?? (updateAvailable ? "update_available" : "latest")
    }
}

struct RuntimeJobInfo {
    let id: String
    let kind: String
    let state: String
    let progress: Double?
    let message: String
    let createdAt: String?
    let errorCode: String?
    let errorMessage: String?

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        kind = value["kind"] as? String ?? "runtime_update"
        state = value["state"] as? String ?? "queued"
        progress = (value["progress"] as? NSNumber)?.doubleValue
        message = value["message"] as? String ?? "処理中"
        createdAt = value["created_at"] as? String
        let error = value["error"] as? [String: Any]
        errorCode = error?["code"] as? String
        errorMessage = error?["message"] as? String
    }

    var isActive: Bool { state == "queued" || state == "running" }
    var isFailed: Bool { state == "failed" }
    var isCancelled: Bool { state == "cancelled" }
    var operationName: String { kind.hasPrefix("runtime_stage:") ? "ランタイムをダウンロード・検証" : "ランタイムを更新" }
}

@MainActor
final class MenuBarViewModel: ObservableObject {
    @Published var serviceRunning = false
    @Published var serviceStatus = "Service stopped"
    @Published var loadedName: String?
    @Published var loadedEngine: String?
    @Published var loadedModalities: [String] = []
    @Published var loadedModelMaxTokens: Int?
    @Published var effectiveMaxTokens = 8192
    @Published var effectiveMaxPromptCharacters = 100000
    @Published var activeRequestCount = 0
    @Published var queuedRequestCount = 0
    @Published var oldestQueuedSeconds = 0
    @Published var loadingModelName: String?
    @Published var loadingEngine: String?
    @Published var loadingPhase: String?
    @Published var loadingStartedAt: Date?
    @Published var modelCopyStatus: String?
    @Published var apiURL = "http://127.0.0.1:11435"
    @Published var localAPIURL = "http://127.0.0.1:11435"
    @Published var lanAPIURLs: [String] = []
    @Published var lanEnabled = false
    @Published var models: [CatalogModel] = []
    @Published var runtimes: [RuntimeInfo] = []
    @Published var runtimeUpdates: [String: RuntimeUpdateInfo] = [:]
    @Published var runtimeProgress: [String: String] = [:]
    @Published var updatingEngines: Set<String> = []
    @Published var checkingRuntimeEngines: Set<String> = []
    @Published var runtimeJobs: [String: RuntimeJobInfo] = [:]
    @Published var busy = false
    @Published var errorMessage: String?
    @Published var chatOutput = ""
    @Published var generationTPS: Double?
    @Published var currentRequestID: String?
    @Published var cancellationInProgress = false
    @Published var cancellationStatus: String?
    @Published var settings: [String: Any] = [:]
    @Published var apiToken = ""
    @Published var lmStudioToken = ""
    @Published var secretStatus: String?
    @Published var isRemovingAllData = false
    @Published var recentLogs: [[String: Any]] = []
    @Published var logStatus: String?
    @Published var guiLanguage = "en" {
        // Views read their strings through `LS(_:)`, which resolves against the
        // language recorded here, so the two must move together.
        didSet { AppLanguage.current = guiLanguage }
    }
    private let client = CoordinatorClient()
    private let cancellationClient = CoordinatorClient()
    private var polling: Task<Void, Never>?
    private var statusRequestToken = 0
    private var started = false
    private var requestedCancellations: Set<String> = []
    private var localLoadInProgress = false
    private var monitoredRuntimeJobIDs: Set<String> = []

    private func ui(_ english: String, _ japanese: String) -> String {
        guiLanguage == "ja" ? japanese : english
    }

    var icon: String {
        if errorMessage != nil { return "exclamationmark.triangle" }
        if busy || activeRequestCount > 0 || queuedRequestCount > 0 { return "waveform" }
        if loadedName != nil { return "cpu.fill" }
        return serviceRunning ? "cpu" : "moon.zzz"
    }
    var shortStatus: String {
        if let loadingModelName { return guiLanguage == "ja" ? "ロード中 · \(loadingModelName)" : "Loading · \(loadingModelName)" }
        if queuedRequestCount > 0 { return guiLanguage == "ja" ? "応答中 · \(queuedRequestCount)件待機" : "Responding · \(queuedRequestCount) queued" }
        if activeRequestCount > 0 { return guiLanguage == "ja" ? "応答中 · \(loadedName ?? "モデル")" : "Responding · \(loadedName ?? "Model")" }
        return loadedName ?? (serviceRunning ? "MLXBar" : (guiLanguage == "ja" ? "停止" : "Stopped"))
    }

    func start() async {
        guard !started else { return }
        started = true
        do { try await client.startService() } catch { errorMessage = error.localizedDescription }
        await refreshAll()
        polling?.cancel()
        polling = Task { [weak self] in
            while !Task.isCancelled {
                let interval = await MainActor.run { NSApp.isActive ? 5 : 30 }
                try? await Task.sleep(for: .seconds(interval))
                guard let self else { return }
                await self.refreshStatus()
            }
        }
    }

    func refreshAll() async {
        await refreshStatus(); await refreshModels(); await refreshRuntimes(); await refreshSettings()
    }

    /// Assigns only when the value actually differs.
    ///
    /// `refreshStatus()` runs every second while the menu is open (see
    /// `MenuBarView.task`) and re-decodes the full status payload on every
    /// tick. `@Published` broadcasts `objectWillChange` on every assignment
    /// regardless of whether the new value equals the old one, so writing
    /// unconditionally here forced the status item's `Label(shortStatus,
    /// systemImage: icon)` to re-render up to a dozen times per second even
    /// when nothing changed. `MenuBarExtra`'s `.menuBarExtraStyle(.window)`
    /// is known (see mlx-bar.md) to render unreliably under rapid content
    /// churn — that's what previously collapsed the popover to a few px in
    /// v1.2.1 — and the same churn here was flashing/hiding the menu bar
    /// icon itself right after opening it. Guarding each field keeps
    /// `objectWillChange` quiet when the poll result matches current state.
    private func setIfChanged<T: Equatable>(_ keyPath: ReferenceWritableKeyPath<MenuBarViewModel, T>, _ newValue: T) {
        if self[keyPath: keyPath] != newValue { self[keyPath: keyPath] = newValue }
    }

    func refreshStatus() async {
        // Two loops poll status (the view model's timer and the open menu's
        // 1-second refresh). Without a token, a slow response from one can land
        // after a newer one and revert the display to stale state.
        statusRequestToken &+= 1
        let token = statusRequestToken
        do {
            guard let json = try await json("GET", "/api/v1/status") as? [String: Any] else { return }
            guard token == statusRequestToken else { return }
            let wasRunning = serviceRunning
            setIfChanged(\.serviceRunning, true)
            // A successful status poll must not erase a model/runtime error the
            // user still needs to read. Only clear an earlier connection error
            // when the service has actually recovered.
            if !wasRunning { setIfChanged(\.errorMessage, nil) }
            setIfChanged(\.serviceStatus, json["service"] as? String == "running"
                ? (guiLanguage == "ja" ? "サービス稼働中" : "Service running")
                : (guiLanguage == "ja" ? "サービス停止" : "Service stopped"))
            setIfChanged(\.activeRequestCount, (json["activeRequestCount"] as? NSNumber)?.intValue ?? 0)
            setIfChanged(\.queuedRequestCount, (json["queuedRequestCount"] as? NSNumber)?.intValue ?? 0)
            setIfChanged(\.oldestQueuedSeconds, (json["oldestQueuedSeconds"] as? NSNumber)?.intValue ?? 0)
            if let loaded = json["loadedModel"] as? [String: Any] {
                setIfChanged(\.loadedName, loaded["name"] as? String)
                setIfChanged(\.loadedEngine, loaded["engine"] as? String)
                setIfChanged(\.loadedModalities, (loaded["capabilities"] as? [String: Any])?["modalities"] as? [String]
                    ?? loaded["modalities"] as? [String] ?? ["text"])
                setIfChanged(\.loadedModelMaxTokens, ((loaded["capabilities"] as? [String: Any])?["modelMaxTokens"] as? NSNumber)?.intValue)
                setIfChanged(\.effectiveMaxTokens, (loaded["effectiveMaxTokens"] as? NSNumber)?.intValue ?? configuredMaxTokens)
                setIfChanged(\.effectiveMaxPromptCharacters, (loaded["effectiveMaxPromptCharacters"] as? NSNumber)?.intValue ?? 100000)
            } else {
                setIfChanged(\.loadedName, nil); setIfChanged(\.loadedEngine, nil); setIfChanged(\.loadedModalities, [])
                setIfChanged(\.loadedModelMaxTokens, nil); setIfChanged(\.effectiveMaxTokens, configuredMaxTokens)
                setIfChanged(\.effectiveMaxPromptCharacters, 100000); setIfChanged(\.activeRequestCount, 0)
                setIfChanged(\.queuedRequestCount, 0); setIfChanged(\.oldestQueuedSeconds, 0)
            }
            if let loading = json["loadingModel"] as? [String: Any] {
                setIfChanged(\.loadingModelName, loading["name"] as? String)
                setIfChanged(\.loadingEngine, loading["engine"] as? String)
                setIfChanged(\.loadingPhase, loading["phase"] as? String)
                if let timestamp = (loading["startedAt"] as? NSNumber)?.doubleValue {
                    setIfChanged(\.loadingStartedAt, Date(timeIntervalSince1970: timestamp))
                }
            } else if !localLoadInProgress {
                setIfChanged(\.loadingModelName, nil)
                setIfChanged(\.loadingEngine, nil); setIfChanged(\.loadingPhase, nil); setIfChanged(\.loadingStartedAt, nil)
            }
            if let api = json["api"] as? [String: Any] {
                setIfChanged(\.apiURL, api["url"] as? String ?? apiURL)
                setIfChanged(\.localAPIURL, api["localUrl"] as? String ?? apiURL)
                setIfChanged(\.lanAPIURLs, api["lanUrls"] as? [String] ?? [])
                setIfChanged(\.lanEnabled, api["lanEnabled"] as? Bool ?? false)
            }
        } catch {
            guard token == statusRequestToken else { return }
            setIfChanged(\.serviceRunning, false)
            setIfChanged(\.serviceStatus, guiLanguage == "ja" ? "サービス停止" : "Service stopped")
            setIfChanged(\.activeRequestCount, 0)
            setIfChanged(\.queuedRequestCount, 0); setIfChanged(\.oldestQueuedSeconds, 0)
            setIfChanged(\.errorMessage, error.localizedDescription)
        }
    }

    func refreshModels() async {
        errorMessage = nil
        do {
            guard let json = try await json("GET", "/api/v1/models") as? [String: Any],
                  let data = json["data"] as? [[String: Any]] else { return }
            models = data.compactMap(CatalogModel.init)
        } catch { errorMessage = error.localizedDescription }
    }

    func scan() async {
        await perform {
            let job = try await self.json("POST", "/api/v1/models/scan") as? [String: Any]
            if let job { _ = try await self.waitForJob(job) }
            await self.refreshModels()
        }
    }

    func load(_ model: CatalogModel, engine: String = "auto") async {
        localLoadInProgress = true
        loadingModelName = model.name
        loadingEngine = engine == "auto" ? model.engine : engine
        loadingPhase = ui("Starting model load", "ロードを開始しています")
        loadingStartedAt = Date()
        await perform {
            _ = try await self.json("POST", "/api/v1/models/\(self.pathComponent(model.id))/load",
                                    ["engine": engine],
                                    timeoutSeconds: CoordinatorClient.Timeout.modelLoad)
            await self.refreshStatus()
        }
        localLoadInProgress = false
        loadingModelName = nil; loadingEngine = nil; loadingPhase = nil; loadingStartedAt = nil
    }

    func unload() async {
        await perform { _ = try await self.json("DELETE", "/api/v1/models/loaded"); await self.refreshStatus() }
    }

    func generate(prompt: String, images: [URL], temperature: Double = 0.7, maxTokens: Int = 512) async {
        guard !prompt.isEmpty else { return }
        chatOutput = ""; busy = true; errorMessage = nil; cancellationStatus = nil
        let requestID = UUID().uuidString
        currentRequestID = requestID
        do {
            let body: [String: Any] = ["prompt": prompt, "images": images.map(\.path),
                                       "temperature": temperature, "max_tokens": maxTokens,
                                       "requestId": requestID]
            try await client.stream("/api/v1/generate", bodyJSON: try self.encode(body)) { [weak self] event in
                guard let self else { return }
                Task { @MainActor in
                    if event.type == "delta", let text = event.text {
                        self.chatOutput += text
                    } else if event.type == "metrics" {
                        self.generationTPS = event.generationTPS
                    } else if event.type == "error" {
                        self.errorMessage = event.message
                    }
                }
            }
        } catch {
            if !requestedCancellations.contains(requestID) {
                errorMessage = error.localizedDescription
            }
        }
        requestedCancellations.remove(requestID)
        currentRequestID = nil; busy = false
    }

    func cancelGeneration() async {
        guard let requestID = currentRequestID, !cancellationInProgress else { return }
        cancellationInProgress = true
        cancellationStatus = ui("Stopping…", "停止処理中…")
        requestedCancellations.insert(requestID)
        defer { cancellationInProgress = false }
        do {
            let data = try await cancellationClient.request("POST", "/api/v1/generate/\(pathComponent(requestID))/cancel")
            let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            if result?["cancelled"] as? Bool == true {
                cancellationStatus = result?["forced"] as? Bool == true
                    ? ui("Generation was force-stopped", "生成を強制停止しました")
                    : ui("Generation stopped", "生成を停止しました")
                if result?["forced"] as? Bool == true { await refreshStatus() }
            } else {
                cancellationStatus = ui("Generation has already finished", "生成はすでに終了しています")
            }
        } catch {
            cancellationStatus = ui("Could not stop generation", "停止に失敗しました")
            errorMessage = error.localizedDescription
        }
    }

    func cancelAllGenerations() async {
        guard !cancellationInProgress else { return }
        cancellationInProgress = true
        cancellationStatus = ui("Stopping all generations…", "すべての生成を停止処理中…")
        defer { cancellationInProgress = false }
        do {
            let data = try await cancellationClient.request("POST", "/api/v1/generate/cancel-all")
            _ = try JSONSerialization.jsonObject(with: data)
            cancellationStatus = ui("Stop requested", "停止を要求しました")
            await refreshStatus()
        } catch {
            cancellationStatus = ui("Could not stop generation", "停止に失敗しました")
            errorMessage = error.localizedDescription
        }
    }

    func refreshRuntimes() async {
        errorMessage = nil
        do {
            guard let json = try await json("GET", "/api/v1/runtimes") as? [String: Any] else { return }
            runtimes = ["mlx-lm", "mlx-vlm"].compactMap { engine in
                guard let value = json[engine] as? [String: Any] else { return nil }
                let active = (value["active"] as? [String: Any])?["active"] as? String
                if let check = value["lastCheck"] as? [String: Any] {
                    runtimeUpdates[engine] = RuntimeUpdateInfo(check)
                }
                return RuntimeInfo(id: engine, active: active,
                                   slots: value["slots"] as? [[String: Any]] ?? [],
                                   history: value["history"] as? [[String: Any]] ?? [],
                                   activeJob: value["activeJob"] as? [String: Any])
            }
            for runtime in runtimes {
                if let job = runtime.activeJob { attachRuntimeJob(runtime.id, job) }
            }
        } catch { errorMessage = error.localizedDescription }
    }

    func refreshRuntimeManager() async {
        await refreshRuntimes()
        async let lmCheck: Void = checkRuntimeUpdate("mlx-lm")
        async let vlmCheck: Void = checkRuntimeUpdate("mlx-vlm")
        _ = await (lmCheck, vlmCheck)
        await refreshRuntimes()
    }

    func stageRuntime(_ engine: String, version: String?) async {
        guard !updatingEngines.contains(engine) else { return }
        errorMessage = nil
        do {
            let trimmed = version?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let selectedVersion: Any = trimmed.isEmpty ? NSNull() : trimmed
            if let job = try await json("POST", "/api/v1/runtimes/\(pathComponent(engine))/stage",
                                        ["version": selectedVersion, "gitRef": NSNull()]) as? [String: Any] {
                attachRuntimeJob(engine, job)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func checkRuntimeUpdate(_ engine: String) async {
        guard !updatingEngines.contains(engine), !checkingRuntimeEngines.contains(engine) else { return }
        checkingRuntimeEngines.insert(engine)
        runtimeProgress[engine] = ui("Checking latest version…", "最新版を確認中…")
        errorMessage = nil
        defer { checkingRuntimeEngines.remove(engine); runtimeProgress.removeValue(forKey: engine) }
        do {
            guard let value = try await json("POST", "/api/v1/runtimes/\(pathComponent(engine))/check") as? [String: Any] else { return }
            runtimeUpdates[engine] = RuntimeUpdateInfo(value)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func updateRuntimeAutomatically(_ engine: String) async {
        guard !updatingEngines.contains(engine) else { return }
        errorMessage = nil
        do {
            if let job = try await json("POST", "/api/v1/runtimes/\(pathComponent(engine))/update") as? [String: Any] {
                attachRuntimeJob(engine, job)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func attachRuntimeJob(_ engine: String, _ value: [String: Any]) {
        guard let info = RuntimeJobInfo(value) else { return }
        runtimeJobs[engine] = info
        guard info.isActive, !monitoredRuntimeJobIDs.contains(info.id) else { return }
        monitoredRuntimeJobIDs.insert(info.id)
        updatingEngines.insert(engine)
        Task { [weak self] in await self?.monitorRuntimeJob(engine, initial: value) }
    }

    private func monitorRuntimeJob(_ engine: String, initial: [String: Any]) async {
        guard let id = initial["id"] as? String else { return }
        defer {
            monitoredRuntimeJobIDs.remove(id)
            updatingEngines.remove(engine)
        }
        do {
            let completed = try await waitForJob(initial) { state in
                if let info = RuntimeJobInfo(state) { self.runtimeJobs[engine] = info }
            }
            if let info = RuntimeJobInfo(completed) { runtimeJobs[engine] = info }
            if let result = completed["result"] as? [String: Any],
               let check = result["check"] as? [String: Any] {
                runtimeUpdates[engine] = RuntimeUpdateInfo(check)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        await refreshRuntimes()
    }

    func activate(_ engine: String, slot: String) async {
        await perform { _ = try await self.json("POST", "/api/v1/runtimes/\(self.pathComponent(engine))/activate", ["slotId": slot]); await self.refreshRuntimes() }
    }

    func rollback(_ engine: String) async {
        await perform { _ = try await self.json("POST", "/api/v1/runtimes/\(self.pathComponent(engine))/rollback"); await self.refreshRuntimes() }
    }

    func deleteRuntimeSlot(_ engine: String, slot: String, version: String?) async {
        await perform {
            _ = try await self.json("DELETE", "/api/v1/runtimes/\(self.pathComponent(engine))/slots/\(self.pathComponent(slot))")
            self.runtimeProgress[engine] = self.ui(
                "Removed \(version.map { "version \($0)" } ?? "the selected runtime")",
                "\(version.map { "バージョン \($0)" } ?? "選択したランタイム")を削除しました")
            await self.refreshRuntimes()
        }
    }

    func cancelRuntimeJob(_ engine: String) async {
        guard let job = runtimeJobs[engine], job.isActive else { return }
        do {
            if let value = try await json("POST", "/api/v1/runtimes/\(pathComponent(engine))/jobs/\(pathComponent(job.id))/cancel") as? [String: Any],
               let info = RuntimeJobInfo(value) {
                runtimeJobs[engine] = info
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func refreshSettings() async {
        do {
            settings = try await json("GET", "/api/v1/settings") as? [String: Any] ?? [:]
            guiLanguage = ((settings["general"] as? [String: Any])?["language"] as? String) ?? "en"
            effectiveMaxTokens = loadedModelMaxTokens.map { min(configuredMaxTokens, $0) } ?? configuredMaxTokens
            await reconcileLaunchAtLogin()
        }
        catch { errorMessage = error.localizedDescription }
    }

    /// Applies `general.launchAtLogin` to the actual OS registration.
    ///
    /// `mlxbarctl` can only change the *setting* (`config set-launch-at-login`)
    /// — `SMAppService` is a Swift/ObjC-only API with no CLI equivalent — so a
    /// change made from the CLI while the GUI wasn't running has no effect on
    /// its own. This reconciles the two whenever settings are refreshed (i.e.
    /// on every GUI launch, at minimum), so a CLI-set desired state still
    /// eventually takes effect. Checking the current status first avoids
    /// calling register()/unregister() when they'd be a no-op.
    private func reconcileLaunchAtLogin() async {
        let desired = (settings["general"] as? [String: Any])?["launchAtLogin"] as? Bool ?? false
        let current = SMAppService.mainApp.status
        if desired, current != .enabled {
            try? SMAppService.mainApp.register()
        } else if !desired, current == .enabled {
            try? await SMAppService.mainApp.unregister()
        }
    }

    var configuredMaxTokens: Int {
        (((settings["generation"] as? [String: Any])?["maxTokens"] as? NSNumber)?.intValue) ?? 8192
    }

    func setGUILanguage(_ language: String) async {
        guard ["en", "ja"].contains(language) else { return }
        guiLanguage = language
        await setConfig("general.language", value: language)
    }

    var configuredTemperature: Double {
        (((settings["generation"] as? [String: Any])?["defaultTemperature"] as? NSNumber)?.doubleValue) ?? 0.7
    }

    var configuredTopP: Double {
        (((settings["generation"] as? [String: Any])?["defaultTopP"] as? NSNumber)?.doubleValue) ?? 1.0
    }

    var configuredRepetitionPenalty: Double {
        (((settings["generation"] as? [String: Any])?["defaultRepetitionPenalty"] as? NSNumber)?.doubleValue) ?? 1.0
    }

    var configuredRepetitionContextSize: Int {
        (((settings["generation"] as? [String: Any])?["repetitionContextSize"] as? NSNumber)?.intValue) ?? 20
    }

    var modelActivityText: String {
        if guiLanguage == "ja" {
            if loadingModelName != nil { return "モデルをロード中" }
            if queuedRequestCount > 0 { return "モデルが応答を生成中 · \(queuedRequestCount)件待機" }
            if activeRequestCount > 0 { return "モデルが応答を生成中" }
            if loadedName != nil { return "待機中" }
            return "モデル未ロード"
        }
        if loadingModelName != nil { return "Loading model" }
        if queuedRequestCount > 0 { return "Model is responding · \(queuedRequestCount) queued" }
        if activeRequestCount > 0 { return "Model is responding" }
        if loadedName != nil { return "Ready" }
        return "No model loaded"
    }

    func setMaxTokenLimit(_ value: Int) async {
        guard 1...2_000_000 ~= value else {
            errorMessage = ui("Maximum tokens must be between 1 and 2,000,000", "Max token上限は1〜2,000,000で指定してください")
            return
        }
        await setConfig("generation.maxTokens", value: value)
        await refreshStatus()
    }

    func setQueueLimits(maximum: Int, timeout: Int) async {
        guard 1...64 ~= maximum else {
            errorMessage = ui("Queue capacity must be between 1 and 64", "生成待ち件数は1〜64で指定してください")
            return
        }
        guard 10...7200 ~= timeout else {
            errorMessage = ui("Maximum wait must be between 10 and 7,200 seconds", "最大待ち時間は10〜7,200秒で指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", [
                "generation": ["maxQueuedRequests": maximum, "queueTimeoutSeconds": timeout]
            ])
            await self.refreshSettings()
        }
    }

    func setSamplingDefaults(temperature: Double, topP: Double,
                             repetitionPenalty: Double, repetitionContextSize: Int) async {
        guard 0...2 ~= temperature else { errorMessage = ui("Temperature must be between 0 and 2", "温度は0〜2で指定してください"); return }
        guard 0...1 ~= topP else { errorMessage = ui("Top P must be between 0 and 1", "Top Pは0〜1で指定してください"); return }
        guard 0.01...2 ~= repetitionPenalty else {
            errorMessage = ui("Repetition penalty must be between 0.01 and 2", "繰り返しペナルティは0.01〜2で指定してください"); return
        }
        guard 1...32768 ~= repetitionContextSize else {
            errorMessage = ui("Penalty context must be between 1 and 32,768 tokens", "ペナルティ対象範囲は1〜32,768 tokensで指定してください"); return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["generation": [
                "defaultTemperature": temperature,
                "defaultTopP": topP,
                "defaultRepetitionPenalty": repetitionPenalty,
                "repetitionContextSize": repetitionContextSize,
            ]])
            await self.refreshSettings()
        }
    }

    static func tokenCount(_ value: Int) -> String {
        value.formatted(.number.grouping(.automatic))
    }

    func refreshSecrets() async {
        errorMessage = nil
        do {
            if let api = try await json("GET", "/api/v1/settings/api-token") as? [String: Any] {
                apiToken = api["token"] as? String ?? ""
            }
            if let lm = try await json("GET", "/api/v1/settings/lm-studio-token") as? [String: Any] {
                lmStudioToken = lm["token"] as? String ?? ""
            }
        } catch { errorMessage = error.localizedDescription }
    }

    func refreshRecentLogs() async {
        errorMessage = nil
        do {
            guard let result = try await json("GET", "/api/v1/logs?limit=500") as? [String: Any] else { return }
            recentLogs = result["data"] as? [[String: Any]] ?? []
            logStatus = recentLogs.isEmpty
                ? ui("No API access has been recorded", "記録されたAPIアクセスはありません")
                : ui("Showing the latest \(recentLogs.count) entries (2,000 retained)", "最新\(recentLogs.count)件を表示中（最大2,000件保存）")
        } catch { errorMessage = error.localizedDescription }
    }

    func clearRecentLogs() async {
        await perform {
            _ = try await self.json("DELETE", "/api/v1/logs")
            self.recentLogs = []
            self.logStatus = self.ui("Logs cleared", "ログを消去しました")
        }
    }

    func copyRecentLogs() {
        let text = recentLogs.map(Self.formatLog).joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        logStatus = ui("Visible logs copied", "表示中のログをコピーしました")
    }

    static func formatLog(_ item: [String: Any]) -> String {
        let created = item["created_at"] as? String ?? "-"
        let method = item["method"] as? String ?? "-"
        let path = item["path"] as? String ?? "-"
        let status = (item["status"] as? NSNumber)?.stringValue ?? "-"
        let duration = (item["duration_ms"] as? NSNumber)?.stringValue ?? "0"
        let scope = item["client_scope"] as? String == "lan" ? "LAN" : "このMac"
        let model = item["model"] as? String ?? "-"
        let messages = (item["message_count"] as? NSNumber)?.stringValue ?? "0"
        let tools = (item["tool_count"] as? NSNumber)?.stringValue ?? "0"
        let error = item["error_code"] as? String
        return "\(created)  \(status)  \(method) \(path)  \(duration)ms  \(scope)  model=\(model) messages=\(messages) tools=\(tools)\(error.map { " error=\($0)" } ?? "")"
    }

    func saveAPIToken(_ token: String) async {
        await perform {
            guard let result = try await self.json("PUT", "/api/v1/settings/api-token", ["token": token]) as? [String: Any] else { return }
            self.apiToken = result["token"] as? String ?? ""
            self.secretStatus = self.ui("API key saved", "APIキーを保存しました")
        }
    }

    func regenerateAPIToken() async {
        await perform {
            guard let result = try await self.json("POST", "/api/v1/settings/api-token/regenerate") as? [String: Any] else { return }
            self.apiToken = result["token"] as? String ?? ""
            self.secretStatus = self.ui("New API key generated", "新しいAPIキーを生成しました")
        }
    }

    func saveLMStudioToken(_ token: String) async {
        await perform {
            guard let result = try await self.json("PUT", "/api/v1/settings/lm-studio-token", ["token": token]) as? [String: Any] else { return }
            self.lmStudioToken = result["token"] as? String ?? ""
            self.secretStatus = self.lmStudioToken.isEmpty
                ? self.ui("LM Studio API key removed", "LM Studio APIキーを削除しました")
                : self.ui("LM Studio API key saved", "LM Studio APIキーを保存しました")
        }
    }

    func setPort(_ port: Int) async {
        await perform {
            _ = try await self.json("POST", "/api/v1/settings/api-listener/test", ["port": port])
            _ = try await self.json("PUT", "/api/v1/settings", ["api": ["port": port]])
            await self.refreshAll()
        }
    }

    func setLANAccess(_ enabled: Bool) async {
        await perform {
            let patch: [String: Any] = [
                "api": ["host": enabled ? "0.0.0.0" : "127.0.0.1", "requireToken": true],
                "security": ["allowLan": enabled],
            ]
            _ = try await self.json("PUT", "/api/v1/settings", patch)
            await self.refreshAll()
            self.secretStatus = enabled
                ? self.ui("LAN access enabled", "LAN公開を有効にしました")
                : self.ui("LAN access disabled", "LAN公開を停止しました")
        }
    }

    func setConfig(_ key: String, value: Any) async {
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", self.nestedPatch(key, value))
            await self.refreshSettings()
        }
    }

    func setLaunchAtLogin(_ enabled: Bool) async {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try await SMAppService.mainApp.unregister() }
            await setConfig("general.launchAtLogin", value: enabled)
        } catch { errorMessage = error.localizedDescription }
    }

    func deleteAllAppDataAndQuit() async {
        guard !isRemovingAllData else { return }
        isRemovingAllData = true
        errorMessage = nil
        polling?.cancel()
        do {
            try await client.removeAllData()
            NSApplication.shared.terminate(nil)
        } catch {
            isRemovingAllData = false
            errorMessage = ui("Could not remove all data: \(error.localizedDescription)",
                              "データを完全に削除できませんでした: \(error.localizedDescription)")
        }
    }

    func addModelFolder(_ url: URL) async {
        var roots = ((settings["models"] as? [String: Any])?["roots"] as? [String]) ?? []
        if !roots.contains(url.path) { roots.append(url.path) }
        await perform { _ = try await self.json("PUT", "/api/v1/settings", ["models": ["roots": roots]]); await self.scan() }
    }

    func removeModelFolder(_ path: String) async {
        var roots = ((settings["models"] as? [String: Any])?["roots"] as? [String]) ?? []
        roots.removeAll { $0 == path }
        await setConfig("models.roots", value: roots)
        await scan()
    }

    func copyAPIURL() { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(apiURL, forType: .string) }

    func copyURL(_ url: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(url, forType: .string)
        secretStatus = ui("Connection URL copied", "接続URLをコピーしました")
    }

    func copyLoadedModelName() {
        guard let loadedName else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(loadedName, forType: .string)
        modelCopyStatus = ui("Model name copied", "モデル名をコピーしました")
        let copiedStatus = modelCopyStatus
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(2))
            guard let self, self.modelCopyStatus == copiedStatus else { return }
            self.modelCopyStatus = nil
        }
    }

    func copyAPIToken() {
        guard !apiToken.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(apiToken, forType: .string)
        secretStatus = ui("API key copied", "APIキーをコピーしました")
    }

    private func json(_ method: String, _ path: String, _ body: Any? = nil,
                      timeoutSeconds: Int = CoordinatorClient.Timeout.standard) async throws -> Any {
        let data = try await client.request(method, path, bodyJSON: body.map { try encode($0) },
                                            timeoutSeconds: timeoutSeconds)
        return try JSONSerialization.jsonObject(with: data)
    }

    private func encode(_ value: Any) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: value, options: [])
        return String(decoding: data, as: UTF8.self)
    }

    private func waitForJob(_ initial: [String: Any], onProgress: (([String: Any]) -> Void)? = nil) async throws -> [String: Any] {
        var job = initial
        let deadline = ContinuousClock.now + .seconds(1800)
        while !["completed", "failed", "cancelled"].contains(job["state"] as? String ?? "") {
            if ContinuousClock.now >= deadline {
                throw ClientError.command(ui("Stopped monitoring: the operation did not finish within 30 minutes",
                                             "処理が30分以内に完了しなかったため監視を終了しました"))
            }
            guard let id = job["id"] as? String else {
                throw ClientError.command(ui("The job has no identifier", "ジョブIDがありません"))
            }
            try await Task.sleep(for: .milliseconds(300))
            job = try await json("GET", "/api/v1/jobs/\(pathComponent(id))") as? [String: Any] ?? job
            onProgress?(job)
        }
        if job["state"] as? String == "failed" {
            let detail = job["error"] as? [String: Any]
            throw ClientError.command(detail?["message"] as? String ?? job["message"] as? String
                                      ?? ui("The operation failed", "処理に失敗しました"))
        }
        return job
    }

    private func nestedPatch(_ key: String, _ value: Any) -> [String: Any] {
        key.split(separator: ".").reversed().reduce(value) { current, part in
            [String(part): current]
        } as? [String: Any] ?? [:]
    }

    private func pathComponent(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? value
    }

    private func perform(_ operation: @escaping @MainActor () async throws -> Void) async {
        busy = true; errorMessage = nil
        do { try await operation() } catch { errorMessage = error.localizedDescription }
        busy = false
    }
}
