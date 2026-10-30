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

/// One model currently resident in the coordinator's model pool.
///
/// The pool has held several models at once since v1.6.2, and `/api/v1/status`
/// reports every one of them in `loadedModels`. The single `loadedModel` field
/// stays the *primary* (most recently used) model for the existing header UI;
/// this type backs the list of all residents.
struct ResidentModel: Identifiable, Hashable {
    let id: String
    let name: String
    let engine: String?
    /// "loading" | "ready" | "evicting" | "external" (LM Studio managed).
    let poolState: String
    let memoryReservationBytes: Int64?
    let activeLeases: Int
    let keepLoaded: Bool
    /// Whether the *saved profile* pins this model (v3.2.0 additive
    /// `profilePinned` in `loadedModels[]`).
    ///
    /// `keepLoaded` has always been `slot.keep_loaded or slot.session_pinned`,
    /// so a model loaded by hand (`pin=True` by default) looked pinned even
    /// though no profile named it — and un-pinning it removed nothing, leaving
    /// the icon on screen (ISS-7). `profilePinned` is the profile-only signal
    /// and is the sole basis for the "固定" label; a session-only pin is shown
    /// as "一時" instead. Coordinators older than v3.2.0 omit the key, where
    /// `keepLoaded` is the only information available, so fall back to it.
    let profilePinned: Bool
    let managedExternally: Bool
    /// Number of same-model replica processes (v1.8.0); 1 for the common case.
    let replicaCount: Int
    let replicaIndex: Int
    /// Tokens per second this model is generating right now, or nil when it is
    /// idle. Reported per-model by `/api/v1/status` `loadedModels[]` (v1.9.0) so
    /// the menu bar can show a rate under every resident, not just the primary.
    let generationTokensPerSecond: Double?
    /// Requests waiting behind this model's own generation lane (v1.6.2 pool
    /// field `laneQueueDepth`), distinct from `activeLeases` -- lets the menu
    /// bar tell "generating" apart from "queued behind another request" per
    /// row (v1.10.0), without any backend change.
    let laneQueueDepth: Int
    /// Wall-clock time when this non-resident slot becomes eligible for
    /// idle eviction (pool `idleExpiresAt`, v1.6.2 field). Nil for pinned /
    /// externally managed models, which never expire. Used only for the
    /// "unloads in N" countdown in the row detail.
    let idleExpiresAt: Date?
    /// Seconds left before this model is unloaded for idleness, computed by the
    /// coordinator (pool `idleSecondsRemaining`, v2.8.0). Preferred over
    /// `idleExpiresAt` because it needs no client-side clock arithmetic and is
    /// already reconciled with any "keep it loaded N more seconds" grant.
    let idleSecondsRemaining: Int?
    /// How many seconds an operator has already added to this model's idle
    /// countdown. Zero/absent means "nothing extended".
    let idleExtendSeconds: Int?
    /// Whether this model holds one of the `maxResidentModels` slots
    /// (pool `resident`, v2.8.0). Absent on coordinators older than v2.8.0,
    /// where every loaded model was resident by definition.
    let resident: Bool
    /// Model-state warnings the coordinator reports for this model (v3.2.0
    /// additive `warnings` in `loadedModels[]`). Empty when nothing is wrong;
    /// the menu row renders each one as an orange line.
    let warnings: [ModelWarning]

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        name = value["name"] as? String ?? id
        engine = value["engine"] as? String
        poolState = value["poolState"] as? String ?? "ready"
        memoryReservationBytes = (value["memoryReservationBytes"] as? NSNumber)?.int64Value
        activeLeases = (value["activeLeases"] as? NSNumber)?.intValue ?? 0
        keepLoaded = value["keepLoaded"] as? Bool ?? false
        profilePinned = (value["profilePinned"] as? Bool) ?? (value["keepLoaded"] as? Bool ?? false)
        managedExternally = value["memoryManagedBy"] != nil || (value["poolState"] as? String) == "external"
        replicaCount = (value["replicaCount"] as? NSNumber)?.intValue ?? 1
        replicaIndex = (value["replicaIndex"] as? NSNumber)?.intValue ?? 0
        laneQueueDepth = (value["laneQueueDepth"] as? NSNumber)?.intValue ?? 0
        if let expires = (value["idleExpiresAt"] as? NSNumber)?.doubleValue, expires > 0 {
            idleExpiresAt = Date(timeIntervalSince1970: expires)
        } else {
            idleExpiresAt = nil
        }
        if let remaining = (value["idleSecondsRemaining"] as? NSNumber)?.doubleValue, remaining >= 0 {
            idleSecondsRemaining = Int(remaining.rounded(.up))
        } else {
            idleSecondsRemaining = nil
        }
        if let extended = (value["idleExtendSeconds"] as? NSNumber)?.doubleValue, extended > 0 {
            idleExtendSeconds = Int(extended.rounded())
        } else {
            idleExtendSeconds = nil
        }
        resident = value["resident"] as? Bool ?? true
        warnings = ModelWarning.list(from: value)
        let rate = (value["generationTokensPerSecond"] as? NSNumber)?.doubleValue
        generationTokensPerSecond = (rate ?? 0) > 0 ? rate : nil
    }

    /// Whether the idle-unload countdown (and its "+N seconds" field) applies
    /// to this row. The coordinator reports `idleSecondsRemaining` only for
    /// models it will actually free; a pinned, keep-loaded or externally
    /// managed model has no deadline to extend.
    var isIdleManaged: Bool {
        !keepLoaded && !managedExternally && poolState != "loading" && idleSecondsRemaining != nil
    }

    /// One of "loading" | "generating" | "queued" | "idle", the four states the
    /// v1.10.0 unified model list distinguishes per row.
    var activityState: String {
        if poolState == "loading" { return "loading" }
        if activeLeases > 0 { return "generating" }
        if laneQueueDepth > 0 { return "queued" }
        return "idle"
    }

    /// Localized status line text for `activityState`.
    func activityText(japanese: Bool) -> String {
        switch activityState {
        case "loading": return japanese ? "ロード中…" : "Loading…"
        case "generating": return japanese ? "応答生成中" : "Generating"
        case "queued": return japanese ? "順番待ち" : "Queued"
        default: return japanese ? "待機中" : "Idle"
        }
    }

    /// SF Symbol + tint for `activityState`, matching the existing header
    /// activity line's palette (orange while busy, green once ready).
    var activitySymbol: (name: String, tint: Color) {
        switch activityState {
        case "loading": return ("arrow.triangle.2.circlepath", .secondary)
        case "generating": return ("waveform.circle.fill", .orange)
        case "queued": return ("clock", .secondary)
        default: return ("checkmark.circle.fill", .green)
        }
    }

    /// Live rate as a short localized string, or nil when the model is idle.
    ///
    /// Coarser above 10 tok/s on purpose, matching the primary rate line: the
    /// status poll refreshes every second and a digit that changes on every
    /// tick is the content churn that destabilises `MenuBarExtra`'s window
    /// style (see mlx-bar.md).
    func generationRateText(japanese: Bool) -> String? {
        guard let rate = generationTokensPerSecond, rate > 0 else { return nil }
        let value = rate >= 10 ? String(Int(rate.rounded())) : String(format: "%.1f", rate)
        return japanese ? "\(value) tok/秒" : "\(value) tok/s"
    }

    /// Seconds remaining until this non-resident model is unloaded for
    /// idleness, or nil when that does not apply (busy, pinned, externally
    /// managed, or no expiry reported).
    ///
    /// Always rendered in seconds: v2.8.0 dropped the old "minutes above 90s"
    /// rounding. The point of this line is that the number the user sees is the
    /// number the coordinator will actually enforce, and switching units at a
    /// threshold made the remaining time look like it stalled. The global
    /// "idle time for unpinned models" setting (30-86400s, live without a
    /// restart) drives the countdown, and `model extend-idle` adds to it.
    func idleCountdownText(japanese: Bool) -> String? {
        guard isIdleManaged, let remaining = idleSecondsRemaining, remaining > 0 else { return nil }
        return japanese ? "あと\(remaining)秒で解放" : "Unloads in \(remaining) sec"
    }
}

/// Outcome of the coordinator's startup resident-model preload.
///
/// The coordinator reports this as the additive `preload` key of
/// `/api/v1/status` (v3.2.0). It is absent on older coordinators, so every
/// access is optional and the GUI simply shows nothing in that case. Without
/// it, a failed preload was only a `logging.warning` on the server (ISS-3):
/// the menu bar showed "no model loaded" with no reason.
struct PreloadStatus: Hashable {
    /// "pending" | "running" | "loaded" | "failed" | "skipped".
    let state: String
    let modelId: String?
    /// Server-side Japanese detail, used only when `code` is unknown to
    /// `CoordinatorErrorText`.
    let reason: String?
    /// Stable machine code (`MEMORY_BUDGET_EXCEEDED`, …), preferred for display.
    let code: String?
    let at: Date?

    init?(_ value: [String: Any]) {
        guard let state = value["state"] as? String else { return nil }
        self.state = state
        modelId = value["modelId"] as? String
        reason = value["reason"] as? String
        code = value["code"] as? String
        if let timestamp = (value["at"] as? NSNumber)?.doubleValue, timestamp > 0 {
            at = Date(timeIntervalSince1970: timestamp)
        } else {
            at = nil
        }
    }
}

/// One model-state warning from `/api/v1/status`.
///
/// v3.2.0 (WP-211) reports these on `loadedModel.warnings` and on every
/// `loadedModels[]` entry — never on a generation response. The only code the
/// coordinator emits today is `VISION_IMAGE_DROPPED`, set when the user
/// explicitly loads a vision-capable catalog model with `mlx-lm`, so attached
/// images would be silently discarded. That is *state*, not a transient error:
/// the GUI shows it until the coordinator stops reporting it and never puts it
/// on a 20-second timer (`errorMessage` is for transient failures only).
struct ModelWarning: Hashable {
    let code: String
    let message: String
    let messageEn: String?
    let engine: String?

    init?(_ value: [String: Any]) {
        guard let code = value["code"] as? String, !code.isEmpty else { return nil }
        self.code = code
        message = value["message"] as? String ?? ""
        messageEn = value["messageEn"] as? String
        engine = value["engine"] as? String
    }

    /// The warnings of one `loadedModel` / `loadedModels[]` payload.
    ///
    /// Only that position is object-shaped. The API's other `warnings` keys
    /// (presets, hardware, runtime probe) are arrays of strings, so decoding
    /// one of those here yields nothing instead of a bogus warning.
    static func list(from value: [String: Any]?) -> [ModelWarning] {
        ((value?["warnings"] as? [[String: Any]]) ?? []).compactMap(ModelWarning.init)
    }

    /// Union of the raw warning payloads of same-model replicas, keyed by
    /// `code` (de-duplicated by position when a code is missing).
    ///
    /// `loadedModels[]` carries one entry per replica, and a warning reported
    /// by any replica belongs to the model row: a later replica that reports
    /// none must not erase an earlier replica's warning.
    static func merge(_ existing: [[String: Any]], _ incoming: [[String: Any]]) -> [[String: Any]] {
        var seen = Set<String>()
        var merged: [[String: Any]] = []
        for raw in existing + incoming {
            let key = (raw["code"] as? String) ?? (raw["message"] as? String) ?? ""
            if seen.insert(key).inserted { merged.append(raw) }
        }
        return merged
    }

    /// Display text in `language` ("ja" or "en").
    ///
    /// The server field for the current language comes first: it is more
    /// specific than the general `ErrorText` entry (that one says "this
    /// runtime" where the server names `mlx-lm`). Only a missing or empty
    /// server field falls back to the error-text table — and, for a code the
    /// table does not know, to the server's message and then the code.
    func text(language: String) -> String {
        let serverText = language == "en" ? (messageEn ?? "") : message
        if !serverText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return serverText
        }
        return CoordinatorErrorText.resolve(code: code, serverMessage: message, language: language)
    }

    /// SF Symbol for this warning: a dropped image has its own glyph, and any
    /// code added later falls back to a generic triangle rather than nothing.
    var symbolName: String {
        code == "VISION_IMAGE_DROPPED" ? "photo.badge.exclamationmark" : "exclamationmark.triangle"
    }
}

/// Model entry from oMLX with routing state.
///
/// v2.10.0 decodes the canonical per-model payload (`memoryBytes`, `pleOffload`,
/// `available`) that `omlx_adapter._model_payload` emits; the v2.9.0 decoder
/// looked for `memoryGB`/`pleOffloadEnabled`, which the server never emitted,
/// so the memory and PLE rows never rendered.
struct OMLXModel: Identifiable, Hashable {
    let id: String
    let name: String
    let engine: String?
    let loaded: Bool
    let memoryGB: Double?
    let generationTPS: Double?
    let mtpEnabled: Bool?
    let pleOffloadEnabled: Bool?
    /// Whether the oMLX server actually serves this model (routing-gate).
    let available: Bool
    var useOMLX: Bool

    init?(_ value: [String: Any], useOMLX: Bool = false) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        self.name = value["name"] as? String ?? id
        self.engine = value["engine"] as? String
        self.loaded = value["loaded"] as? Bool ?? false
        if let bytes = value["memoryBytes"] as? NSNumber {
            memoryGB = bytes.doubleValue / (1024 * 1024 * 1024)
        } else {
            // Older coordinator payloads used the memoryGB shortcut.
            memoryGB = (value["memoryGB"] as? NSNumber)?.doubleValue
        }
        self.generationTPS = (value["generationTokensPerSecond"] as? NSNumber)?.doubleValue
        self.mtpEnabled = value["mtpEnabled"] as? Bool
        self.pleOffloadEnabled = value["pleOffload"] as? Bool ?? (value["pleOffloadEnabled"] as? Bool)
        self.available = value["available"] as? Bool ?? true
        self.useOMLX = useOMLX
    }

    init(id: String, name: String, engine: String?, loaded: Bool, memoryGB: Double?,
         generationTPS: Double?, mtpEnabled: Bool?, pleOffloadEnabled: Bool?, useOMLX: Bool) {
        self.id = id
        self.name = name
        self.engine = engine
        self.loaded = loaded
        self.memoryGB = memoryGB
        self.generationTPS = generationTPS
        self.mtpEnabled = mtpEnabled
        self.pleOffloadEnabled = pleOffloadEnabled
        self.available = true
        self.useOMLX = useOMLX
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
    /// Japanese headline shown while the job runs; the views render their own
    /// English for the same `kind` (see `runtimeJobCard` / `omlxJobCard`).
    var operationName: String {
        switch kind {
        case "omlx_install": return "oMLXをインストール"
        case "omlx_start": return "oMLXサーバーを起動"
        case "omlx_stop": return "oMLXサーバーを停止"
        default: return kind.hasPrefix("runtime_stage:") ? "ランタイムをダウンロード・検証" : "ランタイムを更新"
        }
    }
}

struct RagCollectionInfo: Identifiable, Hashable {
    let name: String
    let documentCount: Int
    let chunkCount: Int
    let dimension: Int?
    let embeddingModel: String?
    var id: String { name }

    init?(_ value: [String: Any]) {
        guard let name = value["name"] as? String else { return nil }
        self.name = name
        documentCount = (value["document_count"] as? NSNumber)?.intValue ?? 0
        chunkCount = (value["chunk_count"] as? NSNumber)?.intValue ?? 0
        dimension = (value["dim"] as? NSNumber)?.intValue
        embeddingModel = value["embedding_model"] as? String
    }
}

struct RagDocumentInfo: Identifiable, Hashable {
    let id: String
    let title: String
    let source: String
    let charCount: Int
    let chunkCount: Int

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        title = value["title"] as? String ?? ""
        source = value["source"] as? String ?? ""
        charCount = (value["char_count"] as? NSNumber)?.intValue ?? 0
        chunkCount = (value["chunk_count"] as? NSNumber)?.intValue ?? 0
    }
}

struct RagQueryHit: Identifiable, Hashable {
    let id = UUID()
    let text: String
    let score: Double
    let documentTitle: String

    init(_ value: [String: Any]) {
        text = value["text"] as? String ?? ""
        score = (value["score"] as? NSNumber)?.doubleValue ?? 0
        documentTitle = value["documentTitle"] as? String ?? ""
    }
}

@MainActor
final class MenuBarViewModel: ObservableObject {
    @Published var serviceRunning = false
    @Published var serviceStatus = "Service stopped"
    /// Whether the last status poll failed, i.e. the coordinator is unreachable.
    ///
    /// This is the *persistent* half of what used to be crammed into
    /// `errorMessage`: an outage lasting minutes cannot be expressed by a
    /// message that self-destructs after 20 seconds, and re-posting it on every
    /// poll (once a second while the menu is open) meant the red text never
    /// went away (ISS-1). Connection loss is now shown by `serviceStatus`
    /// ("サービス停止" / "Service stopped") plus the menu bar icon, and never by
    /// the transient banner. Internal state only — no API surface change.
    @Published private(set) var connectionLost = false
    @Published var loadedName: String?
    @Published var loadedEngine: String?
    @Published var loadedModalities: [String] = []
    @Published var loadedModelMaxTokens: Int?
    /// Model-state warnings from `/api/v1/status` (v3.2.0 additive
    /// `loadedModel.warnings`). Empty on older coordinators and whenever no
    /// warning is in effect; shown as an orange line, never on a timer.
    @Published var modelWarnings: [ModelWarning] = []
    @Published var effectiveMaxTokens = 8192
    @Published var effectiveMaxPromptCharacters = 100000
    @Published var cacheCapability: String?
    @Published var cacheColdStreak = 0
    @Published var cacheLastColdReason: String?
    @Published var cacheAffordableTokens = 0
    @Published var cacheDisabledReason: String?
    @Published var cacheRecentTiers: [String] = []
    /// Most recent context-compression event (v1.10.0), or nil when the last
    /// request either didn't need compression or the feature is off.
    @Published var lastContextCompressionOriginalChars: Int?
    @Published var lastContextCompressionCompressedChars: Int?
    @Published var lastAdaptiveMemoryRatio: Double?
    @Published var lastAdaptiveMemoryFallbackReason: String?
    @Published var activeRequestCount = 0
    @Published var queuedRequestCount = 0
    @Published var oldestQueuedSeconds = 0
    @Published var residentModelCount = 0
    /// Models currently loaded past `maxResidentModels` (v2.8.0). They answer
    /// requests like any resident, do not occupy a resident slot, and are freed
    /// by the idle TTL rather than by an explicit unload.
    @Published var nonResidentModelCount = 0
    @Published var residentModels: [ResidentModel] = []
    /// Latest resident-model preload state from `/api/v1/status` (v3.2.0
    /// additive `preload`). Nil on older coordinators, or before the first poll.
    @Published var preload: PreloadStatus?
    @Published var modelPoolReservedBytes: Int64 = 0
    @Published var modelPoolBudgetBytes: Int64 = 0
    @Published var generationConcurrency = 1
    @Published var activeGenerations = 0
    /// A saved `generationConcurrency` / pool-enabled change that only takes
    /// effect after the next service restart. `nil`-safe: defaults to false on
    /// coordinators that don't report it.
    @Published var modelPoolRestartRequired = false
    /// Human-readable "replica N/M could not load (reason)" when a pinned model
    /// is running below its configured replica count; `nil` when at full width.
    @Published var replicaShortfallSummary: String?
    // Knowledge base (RAG). All read defensively; an older coordinator that
    // omits the `rag` status/settings keys simply leaves these at their
    // defaults and the tab shows the feature as off.
    @Published var ragEnabled = false
    @Published var ragCollections: [RagCollectionInfo] = []
    @Published var ragDocuments: [RagDocumentInfo] = []
    @Published var ragQueryResults: [RagQueryHit] = []
    @Published var ragBackendReachable: Bool?
    @Published var ragBackendMessage: String?
    @Published var ragEmbeddingDim: Int?
    @Published var ragEmbeddingToken = ""
    @Published var ragStatusMessage: String?
    @Published var ragLastRetrievalText: String?
    @Published var loadingModelName: String?
    @Published var loadingEngine: String?
    @Published var loadingPhase: String?
    @Published var loadingStartedAt: Date?
    @Published var modelCopyStatus: String?
    @Published var apiURL = "http://127.0.0.1:11435"
    @Published var localAPIURL = "http://127.0.0.1:11435"
    @Published var lanAPIURLs: [String] = []
    @Published var lanEnabled = false
    @Published var anthropicEnabled = false
    @Published var anthropicURL = ""
    @Published var models: [CatalogModel] = []
    @Published var runtimes: [RuntimeInfo] = []
    @Published var runtimeUpdates: [String: RuntimeUpdateInfo] = [:]
    @Published var runtimeProgress: [String: String] = [:]
    @Published var updatingEngines: Set<String> = []
    @Published var checkingRuntimeEngines: Set<String> = []
    @Published var runtimeJobs: [String: RuntimeJobInfo] = [:]
    // Bulk presets + setup wizard + settings export/import (v2.6.0).
    @Published var presetStatus: [String: Any] = [:]
    @Published var presetMessage: String?
    @Published var setupStatus: [String: Any] = [:]
    @Published var showSetupWizard = false
    @Published var setupMessage: String?
    @Published var exportImportMessage: String?
    @Published var diagnosticsSummary: [String: String] = [:]
    @Published var busy = false
    /// Transient, user-action-scoped error text (auto-dismisses; see
    /// `errorDismissDuration`). Background polling must never write here — use
    /// `connectionLost` for that. `didSet` arms a cancellation-safe timer and
    /// only clears the message it was armed for.
    @Published var errorMessage: String? {
        didSet {
            errorClearTask?.cancel()
            errorClearGeneration &+= 1
            let generation = errorClearGeneration
            guard let message = errorMessage else { return }
            let duration = errorDismissDuration
            errorClearTask = Task { [weak self] in
                // A cancelled sleep must return without clearing: the old
                // `try?` swallowed the CancellationError and fell through to
                // `errorMessage = nil`, which wiped the *newer* error the
                // cancellation was reacting to (ISS-1 / AC-1-2).
                do { try await Task.sleep(for: duration) } catch { return }
                guard let self, self.errorClearGeneration == generation,
                      self.errorMessage == message else { return }
                self.errorMessage = nil
            }
        }
    }
    /// How long a transient `errorMessage` stays on screen.
    ///
    /// A `static let` so the lifetime is defined in one place and no settings
    /// key is added (settings schema stays unchanged). `errorDismissDuration`
    /// is the per-instance copy a test can shorten.
    static let errorAutoDismissDuration: Duration = .seconds(20)
    /// Instance copy of `errorAutoDismissDuration`; production never changes
    /// it, tests inject a short value so they do not sleep for 20 seconds.
    var errorDismissDuration: Duration = MenuBarViewModel.errorAutoDismissDuration
    private var errorClearTask: Task<Void, Never>?
    private var errorClearGeneration = 0
    @Published var chatOutput = ""
    /// Upper bound for `chatOutput`: a long agent reply would otherwise live
    /// for the whole app session as an ever-copied Swift String (plus a full
    /// re-layout per delta). The tail is kept so the visible reply stays
    /// intact; no settings key, no UI string change.
    static let maxChatOutputCharacters = 200_000
    @Published var generationTPS: Double?
    /// Tokens per second of the generation currently running, from the status
    /// poll rather than from a stream this GUI owns -- so it also reflects work
    /// arriving from ZCode or another machine on the LAN.
    @Published var liveGenerationTPS: Double?
    @Published var currentRequestID: String?
    @Published var cancellationInProgress = false
    @Published var cancellationStatus: String?
    @Published var settings: [String: Any] = [:]
    /// True once `/api/v1/settings` has been read successfully at least once.
    /// Pin / replica / per-model memory controls stay disabled until then: a
    /// pin action built from an empty snapshot used to rewrite the whole
    /// `profiles` array from nothing and drop every other residency setting
    /// (ISS-7 / AC-7-4).
    @Published private(set) var settingsLoaded = false
    /// Non-nil when the coordinator had to restore defaults because its config
    /// file could not be read (`settingsRecoveredFrom` in `/api/v1/status`).
    /// Shown once — the recovery name is remembered in
    /// `settingsRecoveryShownFor` — and cleared by
    /// `dismissSettingsRecoveryNotice()` (ISS-4 / AC-4-4).
    @Published var settingsRecoveryNotice: String?
    @Published var apiToken = ""
    @Published var lmStudioToken = ""
    @Published var secretStatus: String?
    @Published var isRemovingAllData = false
    @Published var recentLogs: [[String: Any]] = []
    @Published var logStatus: String?
    @Published var promptCacheStatus: [String: Any] = [:]
    @Published var promptCacheMessage: String?
    @Published var guiLanguage = "en" {
        // Views read their strings through `LS(_:)`, which resolves against the
        // language recorded here, so the two must move together.
        didSet {
            AppLanguage.current = guiLanguage
            // Read-only mirror for the very first frame of the next launch: the
            // settings fetch only completes after the coordinator answers, so
            // without this the menu bar opened in English and then flipped
            // (ISS-4 / AC-4-1). Writing it here keeps the mirror equal to what
            // is actually displayed, and a server response still wins because
            // `applyGUILanguage` assigns this same property (AC-4-5).
            UserDefaults.standard.set(guiLanguage, forKey: Self.guiLanguageDefaultsKey)
        }
    }
    /// `UserDefaults` key holding the last interface language shown.
    static let guiLanguageDefaultsKey = "guiLanguage"
    // MARK: - oMLX
    @Published var omlxEnabled = false
    @Published var omlxServerUrl = "http://127.0.0.1:8000"
    @Published var omlxApiKey = ""
    @Published var omlxStatus = "disconnected" // disconnected, connecting, connected, error
    @Published var omlxVersion = ""
    @Published var omlxError: String?
    @Published var omlxModels: [OMLXModel] = []
    @Published var omlxConnectionTestInProgress = false
    // oMLX setup (v2.10.0): the Runtime page's card drives install/start/stop.
    @Published var omlxRequirements: [String: Any] = [:]
    /// Live job per setup action ("install" / "start" / "stop").
    @Published var omlxSetupJobs: [String: RuntimeJobInfo] = [:]
    /// Actions with a job in flight right now (button disabling).
    @Published var omlxBusySetupActions: Set<String> = []
    private let client = CoordinatorClient()
    private let cancellationClient = CoordinatorClient()
    private var polling: Task<Void, Never>?
    private var statusRequestToken = 0
    private var started = false
    private var requestedCancellations: Set<String> = []
    private var localLoadInProgress = false
    private var monitoredRuntimeJobIDs: Set<String> = []
    /// Recovery name whose banner has already been posted, so a persistent
    /// `settingsRecoveredFrom` in every status poll does not re-show it.
    private var settingsRecoveryShownFor: String?
    /// Handle for the bounded settings-retry loop (at most one at a time).
    private var settingsRetryTask: Task<Void, Never>?
    /// Backoff after a failed first settings read: 3s / 10s / 30s, then stop
    /// (WP-411). Bounded on purpose — an unbounded loop would hammer a
    /// coordinator that is simply not installed.
    static let settingsRetryDelays: [Duration] = [.seconds(3), .seconds(10), .seconds(30)]
    /// Instance copy of `settingsRetryDelays` that a test can shorten.
    var settingsRetryDelaySchedule: [Duration] = MenuBarViewModel.settingsRetryDelays
    /// How many retries the loop has run (exposed for tests).
    private(set) var settingsRetryAttempts = 0

    /// Restores the last interface language before the first frame is drawn.
    ///
    /// `AppLanguage.current` is what every `LS(_:)` call resolves against; a
    /// `didSet` observer does not fire for assignments made inside the
    /// initializer, so it is set explicitly here. No network access: the point
    /// is that the first frame is already right when the coordinator is slow or
    /// absent (AC-4-1 / AC-4-3).
    init() {
        if let stored = UserDefaults.standard.string(forKey: Self.guiLanguageDefaultsKey),
           AppLanguage.supported.contains(stored) {
            guiLanguage = stored
            AppLanguage.current = stored
        }
    }

    /// Applies an interface language from either the coordinator (authoritative)
    /// or a user action. `guiLanguage`'s observer keeps `AppLanguage.current`
    /// and the `UserDefaults` mirror in step.
    func applyGUILanguage(_ language: String) {
        guard AppLanguage.supported.contains(language) else { return }
        guiLanguage = language
    }

    private func ui(_ english: String, _ japanese: String) -> String {
        guiLanguage == "ja" ? japanese : english
    }

    /// Turns any thrown error into a message in the interface language.
    ///
    /// A structured coordinator error (`ClientError.api`) is resolved by its
    /// machine `code` through `CoordinatorErrorText`, so the text stays in the
    /// chosen language even when the underlying failure came from an English-only
    /// upstream runtime. Everything else falls back to its own description, and
    /// truly unexpected errors get a generic sentence rather than a system-locale
    /// string the user set out to avoid.
    func presentError(_ error: Error) -> String {
        switch error {
        case let ClientError.api(code, message):
            return CoordinatorErrorText.resolve(code: code, serverMessage: message, language: guiLanguage)
        case let clientError as ClientError:
            return clientError.errorDescription ?? ui("Something went wrong.", "エラーが発生しました")
        case let urlError as URLError where urlError.code == .cancelled:
            return ui("The request was cancelled.", "リクエストがキャンセルされました")
        default:
            return ui("Communication with the MLXBar service failed.",
                      "MLXBarサービスとの通信に失敗しました")
        }
    }

    var icon: String {
        if errorMessage != nil { return "exclamationmark.triangle" }
        // A lost coordinator is persistent state, not an error the user can
        // dismiss: a slashed-network glyph says so without repainting the
        // icon red forever (the old behaviour re-armed the error text every
        // poll, so the triangle never went away — ISS-1).
        if connectionLost { return "wifi.slash" }
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

    /// Localized line for the coordinator's resident-model preload state, with
    /// whether it is a failure (rendered as a warning, not a transient error).
    ///
    /// Nothing is returned for "loaded" / "skipped": the model list already
    /// tells that story, and a permanent "loaded" line would just add noise to
    /// the popover. The reason prefers the machine code (so English stays
    /// English) and falls back to the server's message for unknown codes.
    var preloadStatusLine: (text: String, failed: Bool)? {
        guard let preload else { return nil }
        switch preload.state {
        case "pending", "running":
            return (LS("常駐モデルを読み込み中…"), false)
        case "failed":
            var reason = CoordinatorErrorText.resolve(
                code: preload.code, serverMessage: preload.reason, language: guiLanguage)
            // Table sentences end with a period and the template adds one back,
            // so drop it here instead of printing "(reason: …limit.).".
            if reason.hasSuffix(".") { reason.removeLast() }
            return (String(format: LS("常駐モデルの読み込みに失敗しました（理由: %@）"), reason), true)
        default:
            return nil
        }
    }

    /// Applies `status.settingsRecoveredFrom`, announcing the recovery at most
    /// once per backup name.
    ///
    /// Split out of `refreshStatus` so the once-only behaviour is testable
    /// without a live coordinator: the key stays in every status response for
    /// the life of the process, and re-announcing it would make the banner
    /// indistinguishable from a permanent red line (AC-4-4).
    func applySettingsRecovery(_ recovered: String?) {
        guard let recovered, !recovered.isEmpty,
              settingsRecoveryShownFor != recovered else { return }
        settingsRecoveryShownFor = recovered
        settingsRecoveryNotice = String(
            format: LS("設定ファイルを読み込めなかったため、初期値に戻しました（バックアップ: %@）"),
            recovered)
    }

    func dismissSettingsRecoveryNotice() {
        settingsRecoveryNotice = nil
    }

    func start() async {
        guard !started else { return }
        started = true
        do { try await client.startService() } catch { errorMessage = presentError(error) }
        await refreshAll()
        // The coordinator can be slower to start than the GUI. Rather than
        // leaving the interface stuck on defaults until the user opens
        // Settings (ISS-4), retry the settings read a bounded number of times
        // in the background (3s / 10s / 30s, stop on success).
        if !settingsLoaded { beginSettingsRetry() }
        await refreshSetupStatus()
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

    /// Starts the bounded settings-retry loop unless one is already running.
    ///
    /// `loader` and `delays` exist so a unit test can exercise the scheduling
    /// (attempt count, stop-on-success, no unbounded loop) without a live
    /// coordinator; the returned task lets a test await the loop's completion
    /// instead of sleeping, which keeps it immune to timer coalescing.
    /// Production always uses `refreshSettings` and `settingsRetryDelaySchedule`
    /// and ignores the return value.
    @discardableResult
    func beginSettingsRetry(delays: [Duration]? = nil,
                            loader: (() async -> Bool)? = nil) -> Task<Void, Never>? {
        guard settingsRetryTask == nil else { return nil }
        let schedule = delays ?? settingsRetryDelaySchedule
        let task = Task { [weak self] in
            for delay in schedule {
                do { try await Task.sleep(for: delay) } catch {
                    self?.settingsRetryTask = nil
                    return
                }
                if Task.isCancelled { break }
                guard let self else { return }
                self.settingsRetryAttempts += 1
                var loaded = false
                if let loader {
                    loaded = await loader()
                } else {
                    loaded = await self.refreshSettings()
                }
                if loaded { break }
            }
            self?.settingsRetryTask = nil
        }
        settingsRetryTask = task
        return task
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
            setIfChanged(\.serviceRunning, true)
            // Recovery belongs to the *connection* channel now. A successful
            // poll clears `connectionLost` and deliberately leaves
            // `errorMessage` alone: an unrelated transient error the user is
            // still reading must not be erased by a background refresh
            // (AC-1-4), and connection loss no longer lives in that channel at
            // all (AC-1-1).
            setIfChanged(\.connectionLost, false)
            setIfChanged(\.serviceStatus, json["service"] as? String == "running"
                ? (guiLanguage == "ja" ? "サービス稼働中" : "Service running")
                : (guiLanguage == "ja" ? "サービス停止" : "Service stopped"))
            setIfChanged(\.activeRequestCount, (json["activeRequestCount"] as? NSNumber)?.intValue ?? 0)
            setIfChanged(\.queuedRequestCount, (json["queuedRequestCount"] as? NSNumber)?.intValue ?? 0)
            setIfChanged(\.oldestQueuedSeconds, (json["oldestQueuedSeconds"] as? NSNumber)?.intValue ?? 0)
            if let pool = json["modelPool"] as? [String: Any] {
                setIfChanged(\.residentModelCount, (pool["residentModelCount"] as? NSNumber)?.intValue
                             ?? (pool["residentCount"] as? NSNumber)?.intValue ?? 0)
                setIfChanged(\.nonResidentModelCount,
                             (pool["nonResidentModelCount"] as? NSNumber)?.intValue ?? 0)
                setIfChanged(\.modelPoolReservedBytes, (pool["reservedBytes"] as? NSNumber)?.int64Value ?? 0)
                setIfChanged(\.modelPoolBudgetBytes, (pool["budgetBytes"] as? NSNumber)?.int64Value ?? 0)
                setIfChanged(\.generationConcurrency,
                             (pool["generationConcurrency"] as? NSNumber)?.intValue ?? 1)
                setIfChanged(\.activeGenerations,
                             (pool["activeGenerations"] as? NSNumber)?.intValue ?? 0)
                setIfChanged(\.modelPoolRestartRequired,
                             (pool["restartRequired"] as? NSNumber)?.boolValue ?? false)
                let shortfalls = (pool["replicaShortfalls"] as? [[String: Any]]) ?? []
                if let first = shortfalls.first,
                   let modelId = first["modelId"] as? String {
                    let ready = (first["ready"] as? NSNumber)?.intValue ?? 0
                    let desired = (first["desired"] as? NSNumber)?.intValue ?? 0
                    // The stable code, not the localized message: language-neutral
                    // and enough to tell a memory backoff from an implementation gap.
                    let reason = (first["code"] as? String) ?? ""
                    setIfChanged(\.replicaShortfallSummary, guiLanguage == "ja"
                        ? "\(modelId): レプリカ \(ready)/\(desired)（\(reason)）"
                        : "\(modelId): \(ready)/\(desired) replicas (\(reason))")
                } else {
                    setIfChanged(\.replicaShortfallSummary, nil)
                }
            }
            setIfChanged(\.liveGenerationTPS, (json["generationTokensPerSecond"] as? NSNumber)?.doubleValue)
            // Every resident model, not just the primary. Older coordinators
            // (pre-v1.6.2) omit `loadedModels`; fall back to the single
            // `loadedModel` so the list still has one row.
            let residentRaw = (json["loadedModels"] as? [[String: Any]])
                ?? [json["loadedModel"]].compactMap { $0 as? [String: Any] }
            // One row per distinct model; same-model replicas collapse into a
            // single row. v2.7.0: aggregate liveness across the replicas, not
            // just the rate -- previously the first replica's `activeLeases`
            // / `laneQueueDepth` decided the row, so a busy second replica
            // was hidden while idle, and a stale first replica pinned the row
            // on "generating" after its work finished.
            var mergedByID: [String: [String: Any]] = [:]
            var modelOrder: [String] = []
            for value in residentRaw {
                guard let id = value["id"] as? String else { continue }
                guard var existing = mergedByID[id] else {
                    mergedByID[id] = value
                    modelOrder.append(id)
                    continue
                }
                let intValue: ([String: Any], String) -> Int = { dict, key in
                    (dict[key] as? NSNumber)?.intValue ?? 0
                }
                existing["activeLeases"] = NSNumber(
                    value: intValue(existing, "activeLeases") + intValue(value, "activeLeases"))
                existing["laneQueueDepth"] = NSNumber(
                    value: intValue(existing, "laneQueueDepth") + intValue(value, "laneQueueDepth"))
                let rateOf: ([String: Any]) -> Double = { dict in
                    (dict["generationTokensPerSecond"] as? NSNumber)?.doubleValue ?? 0
                }
                let bestRate = max(rateOf(existing), rateOf(value))
                if bestRate > 0 {
                    existing["generationTokensPerSecond"] = NSNumber(value: bestRate)
                }
                existing["replicaCount"] = NSNumber(
                    value: max(intValue(existing, "replicaCount"), intValue(value, "replicaCount"), 1))
                let bytesOf: ([String: Any]) -> Int64 = { dict in
                    (dict["memoryReservationBytes"] as? NSNumber)?.int64Value ?? 0
                }
                let totalBytes = bytesOf(existing) + bytesOf(value)
                if totalBytes > 0 {
                    existing["memoryReservationBytes"] = NSNumber(value: totalBytes)
                }
                if (value["keepLoaded"] as? Bool) == true {
                    existing["keepLoaded"] = true
                }
                // Any replica carrying a saved profile pins the row: `profiles`
                // is per model id, not per replica.
                if (value["profilePinned"] as? Bool) == true {
                    existing["profilePinned"] = true
                }
                if value["memoryManagedBy"] != nil {
                    existing["memoryManagedBy"] = value["memoryManagedBy"]
                }
                // Same-model replicas normally report the same warning, but a
                // replica loaded with a different engine may report none. Keep
                // the union so the row never loses a warning because of the
                // order the coordinator listed its replicas in (A2).
                let mergedWarnings = ModelWarning.merge(
                    existing["warnings"] as? [[String: Any]] ?? [],
                    value["warnings"] as? [[String: Any]] ?? [])
                if !mergedWarnings.isEmpty {
                    existing["warnings"] = mergedWarnings
                }
                // Busiest pool state wins so a loading / busy replica is never
                // hidden behind an idle first replica.
                let stateRank: ([String: Any]) -> Int = { dict in
                    switch dict["poolState"] as? String {
                    case "loading": return 3
                    case "evicting": return 2
                    case "external": return 1
                    default: return 0
                    }
                }
                if stateRank(value) > stateRank(existing) {
                    existing["poolState"] = value["poolState"]
                }
                // Earliest expiry among unpinned replicas: the soonest unload
                // the countdown can promise.
                let expiryOf: ([String: Any]) -> Double? = { dict in
                    guard let raw = (dict["idleExpiresAt"] as? NSNumber)?.doubleValue,
                          raw > 0 else { return nil }
                    return raw
                }
                switch (expiryOf(existing), expiryOf(value)) {
                case let (old?, new?):
                    existing["idleExpiresAt"] = NSNumber(value: min(old, new))
                case (nil, let new?):
                    existing["idleExpiresAt"] = NSNumber(value: new)
                default:
                    break
                }
                // Same reasoning for the coordinator-computed seconds (v2.8.0):
                // the row may only promise the soonest deadline in the group.
                let remainingOf: ([String: Any]) -> Double? = { dict in
                    guard let raw = (dict["idleSecondsRemaining"] as? NSNumber)?.doubleValue,
                          raw >= 0 else { return nil }
                    return raw
                }
                switch (remainingOf(existing), remainingOf(value)) {
                case let (old?, new?):
                    existing["idleSecondsRemaining"] = NSNumber(value: min(old, new))
                case (nil, let new?):
                    existing["idleSecondsRemaining"] = NSNumber(value: new)
                default:
                    break
                }
                // A model is non-resident only when *every* replica of it is:
                // one resident replica is enough for the row to count against
                // `maxResidentModels`. Absent keys mean a pre-v2.8.0
                // coordinator, where all replicas were resident.
                let residentOf: ([String: Any]) -> Bool = { dict in
                    dict["resident"] as? Bool ?? true
                }
                existing["resident"] = NSNumber(value: residentOf(existing) && residentOf(value))
                // An extension granted on one replica is reported for the row,
                // but the coordinator applies it to the whole group.
                if let extended = (value["idleExtendSeconds"] as? NSNumber)?.doubleValue, extended > 0 {
                    existing["idleExtendSeconds"] = NSNumber(value: extended)
                }
                mergedByID[id] = existing
            }
            let distinctResidents = modelOrder.compactMap { ResidentModel(mergedByID[$0] ?? [:]) }
            setIfChanged(\.residentModels, distinctResidents)
            if json["modelPool"] == nil {
                setIfChanged(\.residentModelCount, distinctResidents.count)
            }
            if let loaded = json["loadedModel"] as? [String: Any] {
                setIfChanged(\.loadedName, loaded["name"] as? String)
                setIfChanged(\.loadedEngine, loaded["engine"] as? String)
                setIfChanged(\.loadedModalities, (loaded["capabilities"] as? [String: Any])?["modalities"] as? [String]
                    ?? loaded["modalities"] as? [String] ?? ["text"])
                setIfChanged(\.loadedModelMaxTokens, ((loaded["capabilities"] as? [String: Any])?["modelMaxTokens"] as? NSNumber)?.intValue)
                setIfChanged(\.effectiveMaxTokens, (loaded["effectiveMaxTokens"] as? NSNumber)?.intValue ?? configuredMaxTokens)
                setIfChanged(\.effectiveMaxPromptCharacters, (loaded["effectiveMaxPromptCharacters"] as? NSNumber)?.intValue ?? 100000)
                // Model-state warnings (v3.2.0 additive). An unload or a
                // re-load with a different engine drops them, because the key
                // disappears from the response — hence no dismiss button.
                setIfChanged(\.modelWarnings, ModelWarning.list(from: loaded))
            } else {
                setIfChanged(\.loadedName, nil); setIfChanged(\.loadedEngine, nil); setIfChanged(\.loadedModalities, [])
                setIfChanged(\.loadedModelMaxTokens, nil); setIfChanged(\.effectiveMaxTokens, configuredMaxTokens)
                setIfChanged(\.effectiveMaxPromptCharacters, 100000); setIfChanged(\.activeRequestCount, 0)
                setIfChanged(\.queuedRequestCount, 0); setIfChanged(\.oldestQueuedSeconds, 0)
                setIfChanged(\.liveGenerationTPS, nil)
                setIfChanged(\.modelWarnings, [])
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
            if let health = json["promptCacheHealth"] as? [String: Any] {
                setIfChanged(\.cacheCapability, health["capability"] as? String)
                setIfChanged(\.cacheColdStreak, (health["coldStreak"] as? NSNumber)?.intValue ?? 0)
                setIfChanged(\.cacheLastColdReason, health["lastColdReason"] as? String)
                setIfChanged(\.cacheAffordableTokens, (health["affordableTokens"] as? NSNumber)?.intValue ?? 0)
                setIfChanged(\.cacheDisabledReason, health["disabledReason"] as? String)
                setIfChanged(\.cacheRecentTiers, health["recentTiers"] as? [String] ?? [])
            }
            if let compression = json["contextCompression"] as? [String: Any] {
                setIfChanged(\.lastContextCompressionOriginalChars, (compression["originalChars"] as? NSNumber)?.intValue)
                setIfChanged(\.lastContextCompressionCompressedChars, (compression["compressedChars"] as? NSNumber)?.intValue)
            } else {
                setIfChanged(\.lastContextCompressionOriginalChars, nil)
                setIfChanged(\.lastContextCompressionCompressedChars, nil)
            }
            if let adaptive = json["adaptiveMemory"] as? [String: Any] {
                setIfChanged(\.lastAdaptiveMemoryRatio,
                             (adaptive["compression_ratio"] as? NSNumber)?.doubleValue)
                setIfChanged(\.lastAdaptiveMemoryFallbackReason, adaptive["fallback_reason"] as? String)
            } else {
                setIfChanged(\.lastAdaptiveMemoryRatio, nil)
                setIfChanged(\.lastAdaptiveMemoryFallbackReason, nil)
            }
            if let rag = json["rag"] as? [String: Any] {
                setIfChanged(\.ragEnabled, rag["enabled"] as? Bool ?? false)
                if let last = rag["lastRetrieval"] as? [String: Any],
                   let collection = last["collection"] as? String {
                    let passages = (last["passages"] as? NSNumber)?.intValue ?? 0
                    setIfChanged(\.ragLastRetrievalText, guiLanguage == "ja"
                        ? "「\(collection)」から\(passages)件の文脈を取得しました"
                        : "Retrieved \(passages) passage(s) from “\(collection)”")
                } else {
                    setIfChanged(\.ragLastRetrievalText, nil)
                }
            }
            // v3.2.0 additive keys. Both are read defensively, so a coordinator
            // that predates them simply leaves the corresponding state empty
            // instead of failing the whole poll.
            if let value = json["preload"] as? [String: Any] {
                setIfChanged(\.preload, PreloadStatus(value))
            } else {
                setIfChanged(\.preload, nil)
            }
            applySettingsRecovery(json["settingsRecoveredFrom"] as? String)
            if let api = json["api"] as? [String: Any] {
                setIfChanged(\.apiURL, api["url"] as? String ?? apiURL)
                setIfChanged(\.localAPIURL, api["localUrl"] as? String ?? apiURL)
                setIfChanged(\.lanAPIURLs, api["lanUrls"] as? [String] ?? [])
                setIfChanged(\.lanEnabled, api["lanEnabled"] as? Bool ?? false)
                setIfChanged(\.anthropicEnabled, api["anthropicEnabled"] as? Bool ?? false)
                setIfChanged(\.anthropicURL, api["anthropicUrl"] as? String ?? "")
            }
        } catch {
            guard token == statusRequestToken else { return }
            // Persistent state, not a transient banner: the old code wrote
            // `presentError` here on every poll, and the open menu polls once
            // a second, so the red text came back less than a second after the
            // 20-second timer cleared it (ISS-1). The log line is for
            // diagnosis; the UI learns the same fact from `serviceStatus` and
            // the icon.
            if !connectionLost {
                NSLog("MLXBar: status poll failed: %@", String(describing: error))
            }
            setIfChanged(\.connectionLost, true)
            setIfChanged(\.serviceRunning, false)
            setIfChanged(\.serviceStatus, guiLanguage == "ja" ? "サービス停止" : "Service stopped")
            setIfChanged(\.activeRequestCount, 0)
            setIfChanged(\.queuedRequestCount, 0); setIfChanged(\.oldestQueuedSeconds, 0)
            setIfChanged(\.liveGenerationTPS, nil)
        }
    }

    /// Model list refresh. Returns false when the fetch failed, so a *user*
    /// action (rescan) can report it while the background refresh stays quiet:
    /// a failed background refresh is already visible as `connectionLost`.
    @discardableResult
    func refreshModels() async -> Bool {
        do {
            guard let json = try await json("GET", "/api/v1/models") as? [String: Any],
                  let data = json["data"] as? [[String: Any]] else { return true }
            models = data.compactMap(CatalogModel.init)
            return true
        } catch {
            NSLog("MLXBar: model list refresh failed: %@", String(describing: error))
            return false
        }
    }

    // MARK: - oMLX

    func refreshOMLXSettings() async {
        do {
            let settings = try await self.json("GET", "/api/v1/settings") as? [String: Any] ?? [:]
            // v2.10.0 canonical location: `models.omlx` (the flat `omlx`
            // section the v2.9.0 GUI read is gone server-side).
            if let omlx = (settings["models"] as? [String: Any])?["omlx"] as? [String: Any] {
                omlxEnabled = omlx["enabled"] as? Bool ?? false
                omlxServerUrl = omlx["serverUrl"] as? String ?? "http://127.0.0.1:8000"
            }
        } catch {
            NSLog("MLXBar: oMLX settings refresh failed: %@", String(describing: error))
        }
    }

    func refreshOMLXStatus() async {
        omlxStatus = "connecting"
        do {
            guard let json = try await self.json("GET", "/api/v1/omlx/status") as? [String: Any] else {
                omlxStatus = "error"
                omlxError = ui("No response from the oMLX server.", "oMLXサーバーから応答がありません")
                return
            }
            // Canonical top-level payload (v2.10.0): connected / version /
            // error / serverUrl. Echo the server's URL back so a bind the
            // provider corrected stays visible in the settings field.
            if let url = json["serverUrl"] as? String, !url.isEmpty { omlxServerUrl = url }
            omlxVersion = json["version"] as? String ?? ""
            let connected = json["connected"] as? Bool ?? false
            // A successful connection clears the previous failure text: the
            // server can keep echoing the last error after it is answering
            // again, and a red label that outlives its cause is what made the
            // oMLX page look permanently broken.
            omlxError = connected ? nil : (json["error"] as? String)
            let modelsData = (json["models"] as? [[String: Any]]) ?? []
            // Routing lives in `models.omlx.modelRouting` (v2.10.0); the
            // v2.9.0 GUI read a flat `omlx.routing` the server never wrote.
            let settings = try await self.json("GET", "/api/v1/settings") as? [String: Any] ?? [:]
            let routing = (((settings["models"] as? [String: Any])?["omlx"] as? [String: Any])?["modelRouting"] as? [String: Any]) ?? [:]
            omlxModels = modelsData.compactMap { model in
                guard var m = OMLXModel(model) else { return nil }
                // A model the oMLX server cannot serve is never routed, even
                // if the settings key still says so.
                m.useOMLX = ((routing[m.id] as? Bool) ?? false) && m.available
                return m
            }
            resolveOMLXStatus(connected: connected)
        } catch {
            omlxStatus = "error"
            omlxError = presentError(error)
        }
    }

    private func resolveOMLXStatus(connected: Bool) {
        if omlxError != nil, omlxError?.isEmpty == false {
            omlxStatus = "error"
        } else if connected {
            omlxStatus = "connected"
        } else {
            omlxStatus = "disconnected"
        }
    }

    func setOMLXEnabled(_ enabled: Bool) async {
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings",
                                    ["models": ["omlx": ["enabled": enabled]]])
            await self.refreshOMLXSettings()
            if enabled { await self.refreshOMLXStatus() }
        }
    }

    func setOMLXServerUrl(_ url: String) async {
        let trimmed = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("http://") || trimmed.hasPrefix("https://") else {
            errorMessage = ui("Server URL must start with http:// or https://",
                              "サーバーURLは http:// または https:// で始めてください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings",
                                    ["models": ["omlx": ["serverUrl": trimmed]]])
            await self.refreshOMLXSettings()
            await self.refreshOMLXStatus()
        }
    }

    func setOMLXApiKey(_ key: String) async {
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings/omlx-token", ["token": key])
            self.omlxApiKey = key
        }
    }

    func testOMLXConnection() async {
        omlxConnectionTestInProgress = true
        omlxError = nil
        omlxStatus = "connecting"
        defer { omlxConnectionTestInProgress = false }
        do {
            // `/omlx/connect` returns {connected, version, error, serverUrl};
            // there is no `success` key (v2.10.0 canonical payload).
            let result = try await json("POST", "/api/v1/omlx/connect") as? [String: Any] ?? [:]
            if let version = result["version"] as? String, !version.isEmpty { omlxVersion = version }
            if result["connected"] as? Bool == true {
                omlxStatus = "connected"
                await refreshOMLXStatus()
            } else {
                omlxStatus = "error"
                omlxError = result["error"] as? String ?? ui("Connection failed", "接続に失敗しました")
            }
        } catch {
            omlxStatus = "error"
            omlxError = presentError(error)
        }
    }

    func setOMLXModelRouting(modelId: String, useOMLX: Bool) async {
        await perform {
            // The server deep-merges nested objects, so a single-key routing
            // patch is enough — v2.9.0 re-sent the whole routing map, which
            // dropped concurrent edits made elsewhere.
            _ = try await self.json("PUT", "/api/v1/settings",
                                    ["models": ["omlx": ["modelRouting": [modelId: useOMLX]]]])
            await self.refreshOMLXSettings()
            await self.refreshOMLXStatus()
        }
    }

    func loadOMLXModel(_ modelId: String) async {
        await perform {
            _ = try await self.json("POST", "/api/v1/omlx/models/\(self.pathComponent(modelId))/load")
            await self.refreshOMLXStatus()
        }
    }

    func unloadOMLXModel(_ modelId: String) async {
        await perform {
            _ = try await self.json("POST", "/api/v1/omlx/models/\(self.pathComponent(modelId))/unload")
            await self.refreshOMLXStatus()
        }
    }

    func refreshOMLXApiKey() async {
        do {
            if let token = try await json("GET", "/api/v1/settings/omlx-token") as? [String: Any] {
                omlxApiKey = token["token"] as? String ?? ""
            }
        } catch {
            NSLog("MLXBar: oMLX API key refresh failed: %@", String(describing: error))
        }
    }

    // MARK: - oMLX setup (v2.10.0) — install / start / stop from the Runtime page

    func refreshOMLXSetupRequirements() async {
        do {
            omlxRequirements = try await json("GET", "/api/v1/omlx/setup/requirements") as? [String: Any] ?? [:]
        } catch {
            NSLog("MLXBar: oMLX setup requirements refresh failed: %@", String(describing: error))
        }
    }

    func startOMLXSetup(_ action: String) async {
        guard ["install", "start", "stop"].contains(action),
              !omlxBusySetupActions.contains(action) else { return }
        omlxBusySetupActions.insert(action)
        defer { omlxBusySetupActions.remove(action) }
        do {
            guard let job = try await json("POST", "/api/v1/omlx/setup/\(action)") as? [String: Any],
                  let info = RuntimeJobInfo(job) else { return }
            omlxSetupJobs[action] = info
            guard info.isActive else {
                await refreshOMLXSetupRequirements()
                await refreshOMLXStatus()
                return
            }
            await monitorOMLXSetupJob(action, initial: job)
        } catch {
            errorMessage = presentError(error)
            await refreshOMLXSetupRequirements()
        }
    }

    /// Follows a setup job to its terminal state with the shared `waitForJob`
    /// poller, then re-probes requirements and server reachability — an
    /// install may have produced an `omlx` CLI, a start may have made the
    /// server answer.
    private func monitorOMLXSetupJob(_ action: String, initial: [String: Any]) async {
        guard let id = initial["id"] as? String else { return }
        defer {
            omlxBusySetupActions.remove(action)
        }
        do {
            let completed = try await waitForJob(initial) { state in
                if let info = RuntimeJobInfo(state) { self.omlxSetupJobs[action] = info }
            }
            if let info = RuntimeJobInfo(completed) { omlxSetupJobs[action] = info }
        } catch {
            errorMessage = presentError(error)
        }
        await refreshOMLXSetupRequirements()
        await refreshOMLXStatus()
    }

    func cancelOMLXSetup(_ action: String) async {
        guard let job = omlxSetupJobs[action], job.isActive else { return }
        do {
            if let value = try await json("POST",
                                          "/api/v1/omlx/setup/jobs/\(pathComponent(job.id))/cancel") as? [String: Any],
               let info = RuntimeJobInfo(value) {
                omlxSetupJobs[action] = info
            }
        } catch { errorMessage = presentError(error) }
    }

    func scan() async {
        await perform {
            let job = try await self.json("POST", "/api/v1/models/scan") as? [String: Any]
            if let job { _ = try await self.waitForJob(job) }
            // The rescan is a user action, so a failed list refresh has to be
            // reported rather than swallowed (refreshModels itself stays quiet
            // because the polling loop calls it too).
            let refreshed = await self.refreshModels()
            if !refreshed {
                throw ClientError.command(
                    self.ui("The model list could not be refreshed.",
                            "モデル一覧を更新できませんでした"))
            }
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

    func unload(force: Bool = false) async {
        // The coordinator refuses while generations are in flight so a ZCode
        // request is never failed by a stray click. The GUI is the one caller
        // that can ask a person, so it offers the override rather than
        // bypassing the guard by default.
        let path = force ? "/api/v1/models/loaded?force=true" : "/api/v1/models/loaded"
        await perform { _ = try await self.json("DELETE", path); await self.refreshStatus() }
    }

    /// Free one resident model, leaving the rest of the pool alone.
    ///
    /// The coordinator's `POST /models/{id}/unload` only blocks on *that*
    /// model's own in-flight generations (ENGINE_BUSY, surfaced as a localized
    /// message); other models keep serving. `force` cancels this model's
    /// generations first.
    func unloadModel(_ id: String, force: Bool = false) async {
        let base = "/api/v1/models/\(pathComponent(id))/unload"
        await perform {
            _ = try await self.json("POST", force ? base + "?force=true" : base)
            await self.refreshStatus()
        }
    }

    /// Keep a non-resident model loaded for `seconds` longer than the idle TTL
    /// would have allowed (v2.8.0).
    ///
    /// The extension is additive and is consumed by this model's next
    /// generation, so repeating it can never turn into a hidden permanent pin.
    func extendIdle(_ id: String, seconds: Int) async {
        guard seconds > 0 else {
            errorMessage = ui("Enter a number of seconds to add",
                              "加算する秒数を入力してください")
            return
        }
        await perform {
            _ = try await self.json("POST", "/api/v1/models/\(self.pathComponent(id))/idle/extend",
                                    ["seconds": seconds])
            await self.refreshStatus()
        }
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
                        if self.chatOutput.count > Self.maxChatOutputCharacters {
                            self.chatOutput.removeFirst(
                                self.chatOutput.count - Self.maxChatOutputCharacters)
                        }
                    } else if event.type == "metrics" {
                        self.generationTPS = event.generationTPS
                    } else if event.type == "error" {
                        self.errorMessage = CoordinatorErrorText.resolve(
                            code: event.code, serverMessage: event.message, language: self.guiLanguage)
                    }
                }
            }
        } catch {
            if !requestedCancellations.contains(requestID) {
                errorMessage = presentError(error)
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
            errorMessage = presentError(error)
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
            errorMessage = presentError(error)
        }
    }

    func refreshRuntimes() async {
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
        } catch {
            NSLog("MLXBar: runtime list refresh failed: %@", String(describing: error))
        }
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
            errorMessage = presentError(error)
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
            errorMessage = presentError(error)
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
            errorMessage = presentError(error)
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
            errorMessage = presentError(error)
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
            errorMessage = presentError(error)
        }
    }

    /// Reads `/api/v1/settings` and mirrors the authoritative interface
    /// language.
    ///
    /// Returns `false` when the read failed, which is what lets
    /// `beginSettingsRetry()` back off and try again (WP-411) and lets a caller
    /// tell "empty settings" apart from "not fetched yet". A failure is
    /// deliberately not turned into `errorMessage`: this is a background read
    /// against a coordinator that may simply still be starting (ISS-4), and the
    /// retry loop is the remedy — a red banner that reappears is exactly what
    /// ISS-1 is about.
    @discardableResult
    func refreshSettings() async -> Bool {
        do {
            let value = try await json("GET", "/api/v1/settings") as? [String: Any] ?? [:]
            settings = value
            settingsLoaded = true
            // The server wins: if it says "en" the mirror goes back to "en"
            // (AC-4-5). The observer on `guiLanguage` re-writes the
            // UserDefaults mirror, so the two never diverge.
            applyGUILanguage(((value["general"] as? [String: Any])?["language"] as? String) ?? "en")
            effectiveMaxTokens = loadedModelMaxTokens.map { min(configuredMaxTokens, $0) } ?? configuredMaxTokens
            await reconcileLaunchAtLogin()
            return true
        } catch {
            NSLog("MLXBar: settings refresh failed: %@", String(describing: error))
            return false
        }
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
        guard AppLanguage.supported.contains(language) else { return }
        // Applies immediately (and refreshes the mirror) instead of waiting for
        // the round-trip, so the picker never appears stuck.
        applyGUILanguage(language)
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

    /// Rate shown under the activity line, or nil when there is nothing to show.
    ///
    /// Coarser above 10 tok/s on purpose: the value refreshes every second, and
    /// a digit that changes on every poll is exactly the content churn that
    /// destabilises `MenuBarExtra`'s window style (see mlx-bar.md).
    var generationRateText: String? {
        guard activeRequestCount > 0, let rate = liveGenerationTPS, rate > 0 else { return nil }
        let value = rate >= 10 ? String(Int(rate.rounded())) : String(format: "%.1f", rate)
        return guiLanguage == "ja" ? "\(value) tok/秒" : "\(value) tok/s"
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

    /// What prefix reuse this model gets, stated before it matters.
    ///
    /// A hybrid model whose cache cannot be rolled back behaves completely
    /// differently from one that can — minutes rather than seconds whenever a
    /// conversation branches — and until v1.6.0 nothing in the interface said
    /// so. Naming it at a glance is what stops that from being discovered the
    /// hard way.
    var cacheSummaryText: String? {
        guard loadedName != nil, let capability = cacheCapability else { return nil }
        let japanese = guiLanguage == "ja"
        switch capability {
        case "trim":
            return japanese ? "プロンプト再利用: 巻き戻し可" : "Prompt reuse: rollback supported"
        case "checkpoint":
            let tokens = cacheAffordableTokens
            if tokens > 0 {
                return japanese
                    ? "プロンプト再利用: スナップショット方式 · 約\(tokens.formatted())トークンまで保存可"
                    : "Prompt reuse: snapshots · up to about \(tokens.formatted()) tokens"
            }
            return japanese ? "プロンプト再利用: スナップショット方式" : "Prompt reuse: snapshots"
        default:
            return japanese ? "プロンプト再利用: 非対応" : "Prompt reuse: unavailable"
        }
    }

    /// Set when reuse has actually stopped working, with the reason.
    var cacheWarningText: String? {
        guard loadedName != nil else { return nil }
        let japanese = guiLanguage == "ja"
        if let disabled = cacheDisabledReason, disabled == "budget_insufficient" {
            return japanese
                ? "キャッシュ上限が小さく、スナップショットを1件も保存できません。設定のディスク上限を増やしてください"
                : "The cache limit cannot hold a single snapshot. Raise the disk limit in Settings."
        }
        guard cacheColdStreak >= 2 else { return nil }
        let reason = Self.cacheReasonText(cacheLastColdReason, japanese: japanese)
        return japanese
            ? "直近\(cacheColdStreak)件がプロンプト全体の再計算になっています（\(reason)）"
            : "The last \(cacheColdStreak) requests re-processed the whole prompt (\(reason))"
    }

    /// Set right after a request triggered context compression (v1.10.0), so
    /// the model list can say the model's reply is based on a summary of
    /// earlier turns rather than the verbatim conversation.
    var contextCompressionSummaryText: String? {
        guard let original = lastContextCompressionOriginalChars,
              let compressed = lastContextCompressionCompressedChars else { return nil }
        let japanese = guiLanguage == "ja"
        return japanese
            ? "会話を要約して短縮しました（\(MenuBarViewModel.tokenCount(original))→\(MenuBarViewModel.tokenCount(compressed))文字）"
            : "Compacted the conversation (\(MenuBarViewModel.tokenCount(original))→\(MenuBarViewModel.tokenCount(compressed)) chars)"
    }

    /// Set after a request that went through Adaptive Hybrid Context Memory
    /// (v2.2.0), so the model list can say the reply used a condensed context.
    var adaptiveMemorySummaryText: String? {
        if let reason = lastAdaptiveMemoryFallbackReason, !reason.isEmpty {
            return guiLanguage == "ja"
                ? "アダプティブメモリはEXACTにフォールバックしました（\(reason)）"
                : "Adaptive memory fell back to EXACT (\(reason))"
        }
        guard let ratio = lastAdaptiveMemoryRatio, ratio > 0, ratio < 1 else { return nil }
        let percent = Int(((1 - ratio) * 100).rounded())
        return guiLanguage == "ja"
            ? "アダプティブメモリで文脈を約\(percent)%削減しました"
            : "Adaptive memory trimmed the context by ~\(percent)%"
    }

    static func cacheReasonText(_ reason: String?, japanese: Bool) -> String {
        switch reason {
        case "reuse_unsupported":
            return japanese ? "このモデルはキャッシュを巻き戻せません" : "this model cannot roll its cache back"
        case "budget_insufficient":
            return japanese ? "キャッシュ上限が不足" : "the cache limit is too small"
        case "cancelled_previous":
            return japanese ? "前回の生成が中断されました" : "the previous generation was interrupted"
        case "runtime_changed":
            return japanese ? "ランタイムが変わりました" : "the runtime changed"
        case "memory_pressure":
            return japanese ? "メモリ逼迫のため解放されました" : "it was released under memory pressure"
        case "token_ids_unavailable":
            return japanese ? "ランタイムがトークンIDを返しません" : "the runtime does not report token ids"
        case "write_budget_reached":
            return japanese ? "ディスク書き込み上限に達しました" : "the disk write budget is used up"
        case "first_request":
            return japanese ? "最初の要求です" : "this is the first request"
        default:
            return japanese ? "共通部分がありません" : "no shared prefix"
        }
    }

    /// Prompt input ceilings (v2.4.0). Ranges mirror SettingsStore._validate.
    func setPromptInputLimits(maxPromptChars: Int, maxImages: Int, maxImageBytes: Int) async {
        guard 1...10_000_000 ~= maxPromptChars else {
            errorMessage = ui("Maximum prompt characters must be between 1 and 10,000,000",
                              "プロンプト最大文字数は1〜10,000,000で指定してください")
            return
        }
        guard 0...128 ~= maxImages else {
            errorMessage = ui("Maximum images must be between 0 and 128",
                              "画像最大枚数は0〜128で指定してください")
            return
        }
        guard 1...2_147_483_648 ~= maxImageBytes else {
            errorMessage = ui("Maximum image size must be between 1 and 2,147,483,648 bytes",
                              "画像最大バイト数は1〜2,147,483,648で指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["generation": [
                "maxPromptCharacters": maxPromptChars, "maxImages": maxImages,
                "maxImageBytes": maxImageBytes,
            ]])
            await self.refreshSettings()
        }
    }

    /// Generation timeouts in seconds (v2.4.0). Ranges mirror SettingsStore._validate.
    func setGenerationTimeouts(load: Int, tokenIdle: Int, heartbeat: Int,
                               total: Int, cancelGrace: Int) async {
        guard 10...3600 ~= load, 5...600 ~= tokenIdle, 1...30 ~= heartbeat,
              10...7200 ~= total, 1...30 ~= cancelGrace else {
            errorMessage = ui("One or more timeouts are outside the supported range",
                              "タイムアウトに範囲外の値があります")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["generation": [
                "loadTimeoutSeconds": load, "tokenIdleTimeoutSeconds": tokenIdle,
                "streamHeartbeatSeconds": heartbeat, "totalTimeoutSeconds": total,
                "cancelGraceSeconds": cancelGrace,
            ]])
            await self.refreshSettings()
        }
    }

    /// OMLX-style memory guard ratios (v2.4.0). 0 disables the MLX-side wired
    /// or cache limit and keeps the runtime default. wired must not exceed
    /// memory — checked here as well as on the server.
    func setMemoryGuardRatios(memory: Double, wired: Double, cache: Double) async {
        guard (0.5...0.99).contains(memory) else {
            errorMessage = ui("Memory limit ratio must be between 0.5 and 0.99",
                              "メモリ上限比率は0.5〜0.99で指定してください")
            return
        }
        guard (0...0.95).contains(wired) else {
            errorMessage = ui("Wired limit ratio must be between 0 and 0.95",
                              "wired上限比率は0〜0.95で指定してください")
            return
        }
        guard (0...0.5).contains(cache) else {
            errorMessage = ui("Cache limit ratio must be between 0 and 0.5",
                              "キャッシュ上限比率は0〜0.5で指定してください")
            return
        }
        guard wired <= memory else {
            errorMessage = ui("Wired limit ratio must not exceed the memory limit ratio",
                              "wired上限比率はメモリ上限比率を超えられません")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["generation": [
                "memoryLimitRatio": memory, "wiredLimitRatio": wired,
                "cacheLimitRatio": cache,
            ]])
            await self.refreshSettings()
        }
    }

    /// API listener limits (v2.4.0). 0 bytes derives the ceiling from the
    /// generation limits above. Changing the port/host still goes through the
    /// dedicated setPort/setLANAccess paths with their listener checks.
    func setApiLimits(maxRequestBytes: Int, maxConnections: Int) async {
        guard 0...4_294_967_296 ~= maxRequestBytes else {
            errorMessage = ui("Maximum request size must be between 0 and 4,294,967,296 bytes",
                              "最大要求バイト数は0〜4,294,967,296で指定してください")
            return
        }
        guard 1...1024 ~= maxConnections else {
            errorMessage = ui("Maximum connections must be between 1 and 1,024",
                              "最大同時接続数は1〜1,024で指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["api": [
                "maxRequestBytes": maxRequestBytes,
                "maxConcurrentConnections": maxConnections,
            ]])
            await self.refreshSettings()
        }
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

    func setModelPoolSettings(enabled: Bool, maximum: Int, ttl: Int,
                              perModelGB: Int, totalRatio: Double, reserveGB: Int,
                              generationConcurrency: Int, maxReplicas: Int) async {
        guard 1...8 ~= maximum, 30...86400 ~= ttl, 1...512 ~= perModelGB,
              0.5...0.9 ~= totalRatio, 1...128 ~= reserveGB,
              1...8 ~= generationConcurrency, 1...8 ~= maxReplicas else {
            errorMessage = ui("One or more model residency limits are outside the supported range",
                              "モデル常駐設定に範囲外の値があります")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["models": ["pool": [
                "enabled": enabled,
                "maxResidentModels": maximum,
                "idleTTLSeconds": ttl,
                "defaultPerModelMaxGB": perModelGB,
                "totalMemoryRatio": totalRatio,
                "minimumSystemReserveGB": reserveGB,
                "generationConcurrency": generationConcurrency,
                "maxReplicasPerModel": maxReplicas,
            ]]])
            await self.refreshSettings()
        }
    }

    /// Per-concurrent-generation memory head-room (v2.4.0). 0 derives a
    /// conservative value from the per-model limit; otherwise 0.25–32 GB.
    /// Takes effect for lanes started after the change.
    func setGenerationHeadroom(_ gb: Double) async {
        guard gb == 0 || (0.25...32).contains(gb) else {
            errorMessage = ui("Headroom must be 0 (automatic) or between 0.25 and 32 GB",
                              "ヘッドルームは0（自動）または0.25〜32 GBで指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["models": ["pool": [
                "perGenerationHeadroomGB": gb,
            ]]])
            await self.refreshSettings()
        }
    }

    /// Per-model resident memory ceiling for one pinned profile (v2.4.0).
    /// Other profile fields (keepLoaded, replicas) are preserved: the whole
    /// profiles array is rewritten with only this entry's maxMemoryGB changed.
    func setModelMemoryLimit(_ modelId: String, maxGB: Double) async {
        guard (1...512).contains(maxGB) else {
            errorMessage = ui("Per-model memory must be between 1 and 512 GB",
                              "モデルごとの上限は1〜512 GBで指定してください")
            return
        }
        guard settingsLoaded else {
            errorMessage = ui("Loading settings. Try again in a moment.",
                              "設定を読み込み中です。少し待ってからもう一度お試しください")
            return
        }
        // The ceiling only means something for a saved residency profile; the
        // old code refused the same way before rewriting the array.
        let profiles = (settings["models"] as? [String: Any])?["pool"] as? [String: Any]
        let hasProfile = ((profiles?["profiles"] as? [[String: Any]]) ?? []).contains {
            ($0["modelId"] as? String) == modelId && ($0["keepLoaded"] as? Bool) == true
        }
        guard hasProfile else {
            errorMessage = ui("Pin the model as resident first",
                              "先にモデルを常駐指定してください")
            return
        }
        await perform {
            // Per-model merge (v3.2.0): keepLoaded is re-stated (the endpoint
            // requires it) and the rest of this profile's fields are preserved
            // server-side, as are all other models' profiles.
            _ = try await self.json(
                "PUT", "/api/v1/settings/models/pool/profiles/\(self.pathComponent(modelId))",
                ["keepLoaded": true, "maxMemoryGB": maxGB])
            await self.refreshSettings()
        }
    }

    func setContextCompressionSettings(enabled: Bool, triggerPercent: Int, keepTailMessages: Int,
                                       summaryMaxTokens: Int) async {
        guard 50...95 ~= triggerPercent, 2...50 ~= keepTailMessages, 100...4000 ~= summaryMaxTokens else {
            errorMessage = ui("Context compression settings are outside the supported range",
                              "コンテキスト圧縮の設定値が範囲外です")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["contextCompression": [
                "enabled": enabled,
                "triggerRatio": Double(triggerPercent) / 100,
                "keepTailMessages": keepTailMessages,
                "summaryMaxTokens": summaryMaxTokens,
            ]])
            await self.refreshSettings()
        }
    }

    /// Adaptive Hybrid Context Memory (experimental, v2.2.0). The server
    /// rejects enabling this while Context Compression is on (422); `perform`
    /// surfaces that message.
    func setAdaptiveMemorySettings(enabled: Bool, policy: String, triggerPercent: Int,
                                   memoryPressurePercent: Int, keepTailMessages: Int,
                                   maxLatentTokens: Int, maxRetrievedSegments: Int,
                                   verbatimProtection: Bool, softToken: Bool,
                                   fallbackToExact: Bool = true) async {
        guard ["fidelity", "balanced", "memorySaver"].contains(policy),
              50...95 ~= triggerPercent, 50...95 ~= memoryPressurePercent,
              2...50 ~= keepTailMessages, 16...2048 ~= maxLatentTokens,
              1...64 ~= maxRetrievedSegments else {
            errorMessage = ui("Adaptive memory settings are outside the supported range",
                              "アダプティブメモリの設定値が範囲外です")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["experimental": ["adaptiveMemory": [
                "enabled": enabled,
                "policy": policy,
                "triggerRatio": Double(triggerPercent) / 100,
                "memoryPressureRatio": Double(memoryPressurePercent) / 100,
                "keepTailMessages": keepTailMessages,
                "maxLatentTokens": maxLatentTokens,
                "maxRetrievedSegments": maxRetrievedSegments,
                "verbatimProtection": verbatimProtection,
                "softToken": softToken,
                "fallbackToExact": fallbackToExact,
            ]]])
            await self.refreshSettings()
        }
    }

    func setPagedKVCacheSettings(enabled: Bool, diskEnabled: Bool, maximumGB: Int,
                                 branchReuse: Bool) async {
        guard 1...100 ~= maximumGB else {
            errorMessage = ui("Paged KV disk cache must be between 1 and 100 GB",
                              "Paged KVディスクキャッシュは1〜100 GBで指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["experimental": ["pagedKVCache": [
                "enabled": enabled, "diskEnabled": diskEnabled,
                "diskMaxGB": maximumGB, "branchReuse": branchReuse,
            ]]])
            await self.refreshSettings()
            self.promptCacheMessage = self.ui(
                "The setting applies the next time the model worker starts",
                "設定は次にモデルWorkerを起動したときに反映されます")
        }
    }

    func setKVCacheQuantization(mode: String) async {
        guard ["bf16", "8bit", "4bit"].contains(mode) else {
            errorMessage = ui("KV cache mode must be bf16, 8bit or 4bit",
                              "KVキャッシュのモードはbf16/8bit/4bitで指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["experimental": ["kvCacheQuantization": [
                "mode": mode,
            ]]])
            await self.refreshSettings()
            self.promptCacheMessage = self.ui(
                "The setting applies the next time the model worker starts",
                "設定は次にモデルWorkerを起動したときに反映されます")
        }
    }

    // MARK: - Bulk presets (v2.6.0)

    /// Stored mode, effective mode, and both hardware-derived profiles.
    func refreshPresetStatus() async {
        do {
            if let status = try await json("GET", "/api/v1/presets/status") as? [String: Any] {
                presetStatus = status
            }
        } catch { presetMessage = presentError(error) }
    }

    /// Applies light/full through POST /presets/apply; records custom without
    /// changing any setting. Ranges are enforced server-side; the GUI only
    /// mirrors the mode names.
    func applyPreset(mode: String) async {
        guard ["light", "full", "custom"].contains(mode) else {
            errorMessage = ui("Preset must be light, full or custom",
                              "一括設定モードは軽量/フル/カスタムで指定してください")
            return
        }
        await perform {
            if mode == "custom" {
                _ = try await self.json("PUT", "/api/v1/settings", ["presets": ["mode": "custom"]])
                await self.refreshSettings()
                await self.refreshPresetStatus()
                self.presetMessage = self.ui("Recorded as Custom. No settings were changed.",
                                             "カスタムとして記録しました。設定は変更していません。")
                return
            }
            guard let result = try await self.json(
                "POST", "/api/v1/presets/apply", ["mode": mode]) as? [String: Any] else { return }
            await self.refreshSettings()
            await self.refreshPresetStatus()
            let warnings = (result["warnings"] as? [String]) ?? []
            let applied = self.ui("Applied the \(mode) profile.", "プロファイルを適用しました。")
            self.presetMessage = ([applied] + warnings).joined(separator: "\n")
        }
    }

    // MARK: - First-launch setup (v2.6.0)

    /// Polls once at startup; the wizard shows only on fresh installs.
    func refreshSetupStatus() async {
        do {
            if let status = try await json("GET", "/api/v1/setup/status") as? [String: Any] {
                setupStatus = status
                if status["required"] as? Bool == true { showSetupWizard = true }
            }
        } catch { setupMessage = presentError(error) }
    }

    /// Completes the wizard (or records a skip) and hides it.
    func completeSetup(skipped: Bool, choices: [String: Any]? = nil) async {
        await perform {
            var body: [String: Any] = ["skipped": skipped]
            if let choices { body["choices"] = choices }
            _ = try await self.json("POST", "/api/v1/setup/complete", body)
            await self.refreshSettings()
            await self.refreshSetupStatus()
            self.showSetupWizard = false
        }
    }

    // MARK: - Settings export / import (v2.6.0)

    /// Pretty JSON text of GET /settings/export for the save panel.
    func exportSettingsJSON() async -> String? {
        do {
            let export = try await json("GET", "/api/v1/settings/export")
            let data = try JSONSerialization.data(withJSONObject: export,
                                                  options: [.prettyPrinted, .sortedKeys])
            return String(decoding: data, as: UTF8.self)
        } catch { exportImportMessage = presentError(error); return nil }
    }

    /// Posts a file chosen in the GUI to POST /settings/import and reports
    /// what applied and what had to be skipped on this machine.
    func importSettingsFile(_ url: URL) async {
        await perform {
            let raw = try Data(contentsOf: url)
            guard let envelope = try JSONSerialization.jsonObject(with: raw) as? [String: Any] else {
                throw ClientError.command(self.ui("The file is not a settings export.",
                                                  "設定ファイルとして読み込めませんでした"))
            }
            guard let result = try await self.json(
                "POST", "/api/v1/settings/import", ["export": envelope]) as? [String: Any] else { return }
            await self.refreshSettings()
            await self.refreshPresetStatus()
            let applied = (result["applied"] as? [String]) ?? []
            let skipped = (result["skipped"] as? [[String: Any]]) ?? []
            var lines = [self.ui("Applied \(applied.count) settings.",
                                 "\(applied.count)件の設定を適用しました。")]
            if !skipped.isEmpty {
                lines.append(self.ui("\(skipped.count) items were left unchanged:",
                                      "\(skipped.count)件は変更しませんでした:"))
                lines += skipped.prefix(8).map { entry in
                    "• \(entry["key"] as? String ?? "?"): \(entry["reason"] as? String ?? "")"
                }
                if skipped.count > 8 {
                    lines.append(self.ui("…and \(skipped.count - 8) more.",
                                          "…ほか\(skipped.count - 8)件"))
                }
            }
            self.exportImportMessage = lines.joined(separator: "\n")
        }
    }

    // MARK: - Diagnostics summary (v2.6.0)

    /// Version/platform/root snapshot for the Details panel (display only).
    func refreshDiagnosticsSummary() async {
        do {
            guard let diagnostics = try await json("GET", "/api/v1/diagnostics") as? [String: Any] else { return }
            var summary: [String: String] = [:]
            for key in ["version", "python", "platform", "root"] {
                if let value = diagnostics[key] as? String { summary[key] = value }
            }
            diagnosticsSummary = summary
        } catch { logStatus = presentError(error) }
    }

    // MARK: - Knowledge base (RAG)

    /// Config + collection list, no network probe of the embedding backend.
    func refreshRag() async {
        do {
            if let status = try await json("GET", "/api/v1/rag/status?probe=false") as? [String: Any] {
                applyRagStatus(status)
            }
            if let token = try await json("GET", "/api/v1/settings/rag-embedding-token") as? [String: Any] {
                ragEmbeddingToken = token["token"] as? String ?? ""
            }
        } catch {
            NSLog("MLXBar: knowledge-base status refresh failed: %@", String(describing: error))
        }
    }

    /// Contacts the embedding endpoint and reports reachability + dimension.
    func testRagBackend() async {
        ragStatusMessage = ui("Contacting the embedding endpoint…", "埋め込みエンドポイントに接続中…")
        do {
            guard let status = try await json("GET", "/api/v1/rag/status?probe=true",
                                              timeoutSeconds: 60) as? [String: Any] else { return }
            applyRagStatus(status)
            if ragBackendReachable == true {
                ragStatusMessage = ui("Connected. Embedding dimension: \(ragEmbeddingDim ?? 0)",
                                      "接続成功。埋め込み次元: \(ragEmbeddingDim ?? 0)")
            } else {
                ragStatusMessage = ragBackendMessage ?? ui("Could not reach the embedding endpoint.",
                                                           "埋め込みエンドポイントに接続できませんでした。")
            }
        } catch { ragStatusMessage = presentError(error) }
    }

    private func applyRagStatus(_ json: [String: Any]) {
        ragEnabled = json["enabled"] as? Bool ?? false
        ragCollections = (json["collections"] as? [[String: Any]] ?? []).compactMap(RagCollectionInfo.init)
        if let backend = json["backend"] as? [String: Any] {
            ragBackendReachable = backend["reachable"] as? Bool
            ragEmbeddingDim = (backend["dim"] as? NSNumber)?.intValue
            ragBackendMessage = backend["message"] as? String
        }
    }

    func setRagSettings(enabled: Bool, baseURL: String, model: String,
                        chunkSize: Int, chunkOverlap: Int, defaultTopK: Int,
                        maxContextChars: Int, timeoutSeconds: Int = 30,
                        batchSize: Int = 32, maxChunks: Int = 5000) async {
        guard 100...8000 ~= chunkSize, 0...(chunkSize / 2) ~= chunkOverlap,
              1...20 ~= defaultTopK, 500...32000 ~= maxContextChars,
              5...300 ~= timeoutSeconds, 1...256 ~= batchSize,
              100...50000 ~= maxChunks,
              baseURL.hasPrefix("http://") || baseURL.hasPrefix("https://") else {
            errorMessage = ui("One or more knowledge-base settings are outside the supported range",
                              "知識ベースの設定に範囲外の値があります")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["rag": [
                "enabled": enabled,
                "embedding": ["baseUrl": baseURL, "model": model,
                              "timeoutSeconds": timeoutSeconds,
                              "batchSize": batchSize],
                "chunkSize": chunkSize,
                "chunkOverlap": chunkOverlap,
                "defaultTopK": defaultTopK,
                "maxContextChars": maxContextChars,
                "maxChunksPerCollection": maxChunks,
            ]])
            await self.refreshSettings()
            await self.refreshRag()
        }
    }

    func saveRagEmbeddingToken(_ token: String) async {
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings/rag-embedding-token", ["token": token])
            self.ragStatusMessage = self.ui("Embedding API key saved", "埋め込みAPIキーを保存しました")
        }
    }

    func createRagCollection(_ name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        await perform {
            _ = try await self.json("POST", "/api/v1/rag/collections", ["name": trimmed])
            await self.refreshRag()
        }
    }

    func deleteRagCollection(_ name: String) async {
        await perform {
            _ = try await self.json("DELETE", "/api/v1/rag/collections/\(self.pathComponent(name))")
            self.ragDocuments = []
            await self.refreshRag()
        }
    }

    func refreshRagDocuments(collection: String) async {
        do {
            guard let json = try await json("GET",
                "/api/v1/rag/collections/\(pathComponent(collection))/documents") as? [String: Any],
                  let data = json["data"] as? [[String: Any]] else { return }
            ragDocuments = data.compactMap(RagDocumentInfo.init)
        } catch {
            NSLog("MLXBar: RAG document refresh failed: %@", String(describing: error))
        }
    }

    func addRagDocument(collection: String, title: String, text: String) async {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return }
        await ingestRagDocument(collection: collection,
                                payload: ["title": title, "text": body])
    }

    func addRagDocumentFile(collection: String, path: String) async {
        await ingestRagDocument(collection: collection, payload: ["path": path])
    }

    private func ingestRagDocument(collection: String, payload: [String: Any]) async {
        await perform {
            self.ragStatusMessage = self.ui("Embedding the document…", "ドキュメントを埋め込み中…")
            let job = try await self.json("POST",
                "/api/v1/rag/collections/\(self.pathComponent(collection))/documents",
                payload, timeoutSeconds: CoordinatorClient.Timeout.modelLoad) as? [String: Any] ?? [:]
            let finished = try await self.waitForJob(job)
            let result = finished["result"] as? [String: Any]
            let chunks = (result?["chunkCount"] as? NSNumber)?.intValue ?? 0
            self.ragStatusMessage = self.ui("Added \(chunks) chunk(s)", "\(chunks)個のチャンクを追加しました")
            await self.refreshRagDocuments(collection: collection)
            await self.refreshRag()
        }
    }

    func deleteRagDocument(collection: String, documentId: String) async {
        await perform {
            _ = try await self.json("DELETE",
                "/api/v1/rag/collections/\(self.pathComponent(collection))/documents/\(self.pathComponent(documentId))")
            await self.refreshRagDocuments(collection: collection)
            await self.refreshRag()
        }
    }

    func runRagQuery(collection: String, query: String) async {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        await perform {
            let json = try await self.json("POST",
                "/api/v1/rag/collections/\(self.pathComponent(collection))/query",
                ["query": trimmed], timeoutSeconds: 60) as? [String: Any] ?? [:]
            let results = json["results"] as? [[String: Any]] ?? []
            self.ragQueryResults = results.map(RagQueryHit.init)
            if results.isEmpty {
                self.ragStatusMessage = self.ui("No matching passages", "一致する文章はありませんでした")
            }
        }
    }

    /// Add or remove a model from the pool's keep-loaded profile list.  The
    /// preload at the next service start reads this list; an explicit load is
    /// still needed to make it resident right now.
    /// Add or remove a model from the pool's keep-loaded profile list.
    ///
    /// v3.2.0 sends the *per-model* endpoint instead of rewriting the whole
    /// `profiles` array from the GUI's snapshot. The rewrite lost every other
    /// residency setting whenever the snapshot was empty or stale (ISS-7 /
    /// AC-7-4), and it could not express "remove just this model" at all. The
    /// server merges by `modelId`; `keepLoaded: false` removes the entry.
    func setModelPin(_ modelId: String, keepLoaded: Bool) async {
        guard settingsLoaded else {
            errorMessage = ui("Loading settings. Try again in a moment.",
                              "設定を読み込み中です。少し待ってからもう一度お試しください")
            return
        }
        await perform {
            _ = try await self.json(
                "PUT", "/api/v1/settings/models/pool/profiles/\(self.pathComponent(modelId))",
                ["keepLoaded": keepLoaded])
            var loadError: Error?
            if keepLoaded {
                // Make it resident now too, not just at the next service start.
                // Model loads legitimately take minutes, so use the load
                // timeout. No `try?` here: a pin whose load failed used to be
                // silent, leaving the user with a pinned model that is not
                // running and no explanation (AC-7-5).
                do {
                    _ = try await self.json(
                        "POST", "/api/v1/models/\(self.pathComponent(modelId))/load",
                        ["engine": "auto"], timeoutSeconds: CoordinatorClient.Timeout.modelLoad)
                } catch {
                    loadError = error
                }
            }
            await self.refreshSettings()
            await self.refreshStatus()
            // Re-thrown so `perform` puts the localized reason in
            // `errorMessage`; the settings/status refresh above still ran, so
            // the pin itself shows as applied.
            if let loadError { throw loadError }
        }
    }

    /// Set the same-model replica count for a pinned model (v1.8.0). The extra
    /// replicas come up at the next service start (or when the reaper tops the
    /// model up); 1 keeps the single-process default.
    func setModelReplicas(_ modelId: String, replicas: Int) async {
        guard 1...8 ~= replicas else {
            errorMessage = ui("Replicas must be between 1 and 8", "並列数は1〜8で指定してください")
            return
        }
        guard settingsLoaded else {
            errorMessage = ui("Loading settings. Try again in a moment.",
                              "設定を読み込み中です。少し待ってからもう一度お試しください")
            return
        }
        await perform {
            // Per-model merge (v3.2.0): only this model's entry is touched, so
            // a stale snapshot cannot drop another model's residency settings.
            _ = try await self.json(
                "PUT", "/api/v1/settings/models/pool/profiles/\(self.pathComponent(modelId))",
                ["keepLoaded": true, "replicas": replicas])
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
        do {
            if let api = try await json("GET", "/api/v1/settings/api-token") as? [String: Any] {
                apiToken = api["token"] as? String ?? ""
            }
            if let lm = try await json("GET", "/api/v1/settings/lm-studio-token") as? [String: Any] {
                lmStudioToken = lm["token"] as? String ?? ""
            }
        } catch {
            NSLog("MLXBar: secret refresh failed: %@", String(describing: error))
        }
    }

    func refreshPromptCache() async {
        do {
            promptCacheStatus = try await json("GET", "/api/v1/prompt-cache") as? [String: Any] ?? [:]
        } catch {
            promptCacheMessage = presentError(error)
        }
    }

    func setPromptCacheSettings(enabled: Bool, maximumGB: Int) async {
        guard 1...100 ~= maximumGB else {
            errorMessage = ui("Disk cache must be between 1 and 100 GB",
                              "ディスクキャッシュは1〜100 GBで指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["promptCache": [
                "diskEnabled": enabled, "diskMaxGB": maximumGB,
            ]])
            await self.refreshSettings()
            self.promptCacheMessage = self.ui(
                "The setting applies the next time the model worker starts",
                "設定は次にモデルWorkerを起動したときに反映されます")
        }
    }

    /// Advanced prompt-cache tuning (v2.4.0). Ranges mirror
    /// SettingsStore._validate; the write budget note (one snapshot of a
    /// long conversation costs gigabytes) is stated in the Settings view.
    func setPromptCacheAdvanced(keepGenerations: Int, memoryRatio: Double,
                                branchCheckpoint: String, writeBudgetGB: Double) async {
        guard 1...10 ~= keepGenerations else {
            errorMessage = ui("Generations to keep must be between 1 and 10",
                              "世代保持数は1〜10で指定してください")
            return
        }
        guard (0...0.5).contains(memoryRatio) else {
            errorMessage = ui("Memory ratio must be between 0 and 0.5",
                              "メモリ比率は0〜0.5で指定してください")
            return
        }
        guard ["auto", "off"].contains(branchCheckpoint) else {
            errorMessage = ui("Branch checkpoint must be auto or off",
                              "分岐チェックポイントはautoかoffで指定してください")
            return
        }
        guard (0...4096).contains(writeBudgetGB) else {
            errorMessage = ui("Write budget must be between 0 and 4,096 GB",
                              "書き込み予算は0〜4,096 GBで指定してください")
            return
        }
        await perform {
            _ = try await self.json("PUT", "/api/v1/settings", ["promptCache": [
                "keepGenerations": keepGenerations, "memoryRatio": memoryRatio,
                "branchCheckpoint": branchCheckpoint, "diskWriteBudgetGB": writeBudgetGB,
            ]])
            await self.refreshSettings()
            self.promptCacheMessage = self.ui(
                "The setting applies the next time the model worker starts",
                "設定は次にモデルWorkerを起動したときに反映されます")
        }
    }

    func clearPromptCache(memory: Bool) async {
        await perform {
            let endpoint = memory ? "/api/v1/prompt-cache/memory/clear"
                                  : "/api/v1/prompt-cache/disk/clear"
            self.promptCacheStatus = try await self.json("POST", endpoint) as? [String: Any] ?? [:]
            self.promptCacheMessage = memory
                ? self.ui("Memory prompt cache cleared", "メモリーキャッシュを消去しました")
                : self.ui("Disk prompt cache cleared", "ディスクキャッシュを消去しました")
        }
    }

    func clearPagedPromptCache() async {
        await perform {
            self.promptCacheStatus = try await self.json(
                "POST", "/api/v1/prompt-cache/paged/clear") as? [String: Any] ?? [:]
            self.promptCacheMessage = self.ui(
                "Paged KV cache cleared", "Paged KVキャッシュを消去しました")
        }
    }

    func refreshRecentLogs() async {
        do {
            guard let result = try await json("GET", "/api/v1/logs?limit=200") as? [String: Any] else { return }
            recentLogs = result["data"] as? [[String: Any]] ?? []
            logStatus = recentLogs.isEmpty
                ? ui("No API access has been recorded", "記録されたAPIアクセスはありません")
                : ui("Showing the latest \(recentLogs.count) entries (2,000 retained)", "最新\(recentLogs.count)件を表示中（最大2,000件保存）")
        } catch {
            // Page-scoped status line rather than the transient banner: this is
            // a refresh, and the log pane is the right place for its own
            // failure (errorMessage is reserved for user actions).
            logStatus = presentError(error)
        }
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
        } catch { errorMessage = presentError(error) }
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
        copyModelName(loadedName)
    }

    /// Copies any resident model's name, not just the primary one -- each row
    /// in the v1.10.0 unified model list has its own copy button.
    func copyModelName(_ name: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(name, forType: .string)
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
        do { try await operation() } catch { errorMessage = presentError(error) }
        busy = false
    }
}
