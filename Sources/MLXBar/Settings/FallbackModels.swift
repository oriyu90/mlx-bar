import Foundation

/// Decoded `apiFallback` document (v5.0.0, DESIGN_v5.0.0.md).
///
/// The coordinator owns the shape and validates it; this type only mirrors it so
/// the Settings page can edit one field at a time without re-encoding the whole
/// document by hand. Every field is optional in the JSON, so a document written
/// by a newer build still decodes (missing values fall back to the defaults the
/// coordinator would use).
struct FallbackSettings: Equatable {
    var enabled: Bool = false
    var cooldownSeconds: Int = 60
    var maxAttempts: Int = 3
    var forwardImagesImmediately: Bool = true
    var providers: [FallbackProvider] = []

    init() {}

    init?(_ value: [String: Any]?) {
        guard let value else { return nil }
        enabled = value["enabled"] as? Bool ?? false
        cooldownSeconds = (value["cooldownSeconds"] as? NSNumber)?.intValue ?? 60
        maxAttempts = (value["maxAttempts"] as? NSNumber)?.intValue ?? 3
        forwardImagesImmediately = value["forwardImagesImmediately"] as? Bool ?? true
        providers = (value["providers"] as? [[String: Any]] ?? []).compactMap(FallbackProvider.init)
    }

    func provider(id: String) -> FallbackProvider? {
        providers.first { $0.id == id }
    }
}

struct FallbackProvider: Identifiable, Equatable {
    var id: String
    var name: String
    var protocolName: String
    var baseURL: String
    var enabled: Bool
    var timeoutSeconds: Int
    var apiKeyConfigured: Bool
    var models: [FallbackModel]

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        name = value["name"] as? String ?? ""
        protocolName = value["protocol"] as? String ?? "openai-chat"
        baseURL = value["baseURL"] as? String ?? ""
        enabled = value["enabled"] as? Bool ?? true
        timeoutSeconds = (value["timeoutSeconds"] as? NSNumber)?.intValue ?? 120
        apiKeyConfigured = value["apiKeyConfigured"] as? Bool ?? false
        models = (value["models"] as? [[String: Any]] ?? []).compactMap(FallbackModel.init)
    }

    /// Whether the settings are complete enough to send a request.
    var isUsable: Bool {
        enabled && !baseURL.isEmpty && !models.filter(\.enabled).isEmpty
    }
}

struct FallbackModel: Identifiable, Equatable {
    var id: String
    var model: String
    var label: String
    var enabled: Bool
    var priority: Int
    var sourceModels: [String]
    var sourceModelsExplicit: Bool
    var supportsImages: Bool
    var maxRequests: Int
    var rateWindowSeconds: Int
    var maxOutputTokens: Int
    var maxContextTokens: Int
    var imageDetail: String
    var temperature: Double?
    var topP: Double?
    /// Extra request headers, sent with every call to this model.
    var extraHeaders: [String: String]

    /// `*` in `sourceModels` means "every MLXBar model".
    static let wildcard = "*"

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String else { return nil }
        self.id = id
        model = value["model"] as? String ?? ""
        label = value["label"] as? String ?? ""
        enabled = value["enabled"] as? Bool ?? true
        priority = (value["priority"] as? NSNumber)?.intValue ?? 10
        sourceModels = value["sourceModels"] as? [String] ?? []
        sourceModelsExplicit = value["sourceModelsExplicit"] as? Bool ?? false
        supportsImages = value["supportsImages"] as? Bool ?? false
        maxRequests = (value["maxRequests"] as? NSNumber)?.intValue ?? 0
        rateWindowSeconds = (value["rateWindowSeconds"] as? NSNumber)?.intValue ?? 60
        maxOutputTokens = (value["maxOutputTokens"] as? NSNumber)?.intValue ?? 0
        maxContextTokens = (value["maxContextTokens"] as? NSNumber)?.intValue ?? 0
        imageDetail = value["imageDetail"] as? String ?? "auto"
        temperature = (value["temperature"] as? NSNumber)?.doubleValue
        topP = (value["topP"] as? NSNumber)?.doubleValue
        extraHeaders = value["extraHeaders"] as? [String: String] ?? [:]
    }

    var displayName: String { label.isEmpty ? model : label }

    /// True when this entry is assigned to every MLXBar model.
    var coversAllModels: Bool { sourceModels.contains(Self.wildcard) }

    /// The explicitly named source models (everything except `*`).
    var explicitSources: [String] { sourceModels.filter { $0 != Self.wildcard } }

    /// Whether a given MLXBar model id is assigned to this entry.
    func isAssigned(to modelId: String) -> Bool {
        coversAllModels || explicitSources.contains { $0.caseInsensitiveCompare(modelId) == .orderedSame }
    }
}

/// Protocol identifiers, with their display keys.
enum FallbackProtocol {
    static let all = ["openai-chat", "openai-responses", "anthropic-messages"]

    /// The Japanese label doubles as the localization key (`LS()`).
    static func labelKey(_ protocolName: String) -> String {
        switch protocolName {
        case "openai-responses": return "OpenAI Responses API"
        case "anthropic-messages": return "Anthropic Messages API"
        default: return "OpenAI Chat Completions API"
        }
    }
}

/// One model as reported by `/api/v1/fallback/status` (usage + cooldown).
struct FallbackUsage: Equatable {
    let providerId: String
    let modelId: String
    let usedInWindow: Int
    let coolingDown: Bool

    static func parse(_ status: [String: Any]?) -> [FallbackUsage] {
        guard let providers = status?["providers"] as? [[String: Any]] else { return [] }
        var result: [FallbackUsage] = []
        for provider in providers {
            guard let providerId = provider["id"] as? String else { continue }
            for model in provider["models"] as? [[String: Any]] ?? [] {
                guard let modelId = model["id"] as? String else { continue }
                result.append(FallbackUsage(
                    providerId: providerId, modelId: modelId,
                    usedInWindow: (model["usedInWindow"] as? NSNumber)?.intValue ?? 0,
                    coolingDown: model["coolingDown"] as? Bool ?? false))
            }
        }
        return result
    }
}

/// The last fallback that happened, for the page's status line.
struct FallbackOutcome: Equatable {
    let reason: String?
    let provider: String?
    let model: String?
    let failed: Bool

    init?(_ value: [String: Any]?) {
        guard let value else { return nil }
        reason = value["reason"] as? String
        provider = value["provider"] as? String
        model = value["model"] as? String
        failed = value["failed"] as? Bool ?? false
    }
}
