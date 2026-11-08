import XCTest
@testable import MLXBar

/// Decoding of the v5.0.0 `apiFallback` document (DESIGN_v5.0.0.md).
///
/// The coordinator owns validation, but the Settings page decides what to show
/// from these decoded values -- "All models", "No source model selected", the
/// per-model usage counter. A decoding mistake there would silently present the
/// wrong assignment, so the pure logic is pinned here.
final class FallbackSettingsTests: XCTestCase {
    private func providerJSON(models: [[String: Any]], enabled: Bool = true) -> [String: Any] {
        ["id": "p-00000001", "name": "OpenAI", "protocol": "openai-chat",
         "baseURL": "https://api.openai.com/v1", "enabled": enabled,
         "timeoutSeconds": 120, "apiKeyConfigured": true, "models": models]
    }

    private func modelJSON(_ overrides: [String: Any] = [:]) -> [String: Any] {
        var value: [String: Any] = [
            "id": "m-00000001", "model": "gpt-4.1-mini", "label": "", "enabled": true,
            "priority": 10, "sourceModels": ["*"], "sourceModelsExplicit": false,
            "supportsImages": false, "maxRequests": 0, "rateWindowSeconds": 60,
            "maxOutputTokens": 0, "maxContextTokens": 0, "imageDetail": "auto",
        ]
        for (key, item) in overrides { value[key] = item }
        return value
    }

    func testDecodesDocumentAndDefaults() throws {
        let settings = try XCTUnwrap(FallbackSettings([
            "enabled": true, "cooldownSeconds": 30, "maxAttempts": 5,
            "forwardImagesImmediately": false,
            "providers": [providerJSON(models: [modelJSON()])],
        ]))
        XCTAssertTrue(settings.enabled)
        XCTAssertEqual(settings.cooldownSeconds, 30)
        XCTAssertEqual(settings.maxAttempts, 5)
        XCTAssertFalse(settings.forwardImagesImmediately)
        XCTAssertEqual(settings.providers.count, 1)
        XCTAssertEqual(settings.providers[0].models.count, 1)
        XCTAssertTrue(settings.providers[0].apiKeyConfigured)
    }

    func testMissingKeysFallBackToCoordinatorDefaults() throws {
        let settings = try XCTUnwrap(FallbackSettings(["providers": []]))
        XCTAssertFalse(settings.enabled)
        XCTAssertEqual(settings.cooldownSeconds, 60)
        XCTAssertEqual(settings.maxAttempts, 3)
        XCTAssertTrue(settings.forwardImagesImmediately)
    }

    func testNilDocumentGivesNil() {
        XCTAssertNil(FallbackSettings(nil))
    }

    func testWildcardMeansAllModels() throws {
        let model = try XCTUnwrap(FallbackModel(modelJSON()))
        XCTAssertTrue(model.coversAllModels)
        XCTAssertTrue(model.explicitSources.isEmpty)
        XCTAssertTrue(model.isAssigned(to: "any-model"))
    }

    func testExplicitSourcesAndWildcardTogether() throws {
        let model = try XCTUnwrap(FallbackModel(modelJSON(
            ["sourceModels": ["*", "qwen-9b"], "sourceModelsExplicit": true])))
        XCTAssertTrue(model.coversAllModels)
        XCTAssertEqual(model.explicitSources, ["qwen-9b"])
        XCTAssertTrue(model.isAssigned(to: "qwen-9b"))
    }

    func testExplicitOnlyAssignment() throws {
        let model = try XCTUnwrap(FallbackModel(modelJSON(
            ["sourceModels": ["Qwen-9B"], "sourceModelsExplicit": true])))
        XCTAssertFalse(model.coversAllModels)
        // Case-insensitive, like the coordinator's selector.
        XCTAssertTrue(model.isAssigned(to: "qwen-9b"))
        XCTAssertFalse(model.isAssigned(to: "llama"))
    }

    func testEmptySourcesAreNotAssignedToAnything() throws {
        let model = try XCTUnwrap(FallbackModel(modelJSON(["sourceModels": []])))
        XCTAssertFalse(model.coversAllModels)
        XCTAssertTrue(model.explicitSources.isEmpty)
        XCTAssertFalse(model.isAssigned(to: "qwen"))
    }

    func testDisplayNameFallsBackToModelName() throws {
        let plain = try XCTUnwrap(FallbackModel(modelJSON()))
        XCTAssertEqual(plain.displayName, "gpt-4.1-mini")
        let labelled = try XCTUnwrap(FallbackModel(modelJSON(["label": "Fast"])))
        XCTAssertEqual(labelled.displayName, "Fast")
    }

    func testProviderUsability() throws {
        let usable = try XCTUnwrap(FallbackProvider(providerJSON(models: [modelJSON()])))
        XCTAssertTrue(usable.isUsable)
        let disabled = try XCTUnwrap(FallbackProvider(providerJSON(models: [modelJSON()],
                                                                   enabled: false)))
        XCTAssertFalse(disabled.isUsable)
        let noModels = try XCTUnwrap(FallbackProvider(providerJSON(models: [])))
        XCTAssertFalse(noModels.isUsable)
    }

    func testUsageParsing() {
        let usage = FallbackUsage.parse([
            "providers": [[
                "id": "p-00000001",
                "models": [["id": "m-00000001", "usedInWindow": 3, "coolingDown": true]],
            ]],
        ])
        XCTAssertEqual(usage.count, 1)
        XCTAssertEqual(usage[0].usedInWindow, 3)
        XCTAssertTrue(usage[0].coolingDown)
    }

    func testOutcomeParsing() throws {
        let outcome = try XCTUnwrap(FallbackOutcome(["reason": "QUEUE_FULL",
                                                     "provider": "OpenAI",
                                                     "model": "gpt-4.1-mini"]))
        XCTAssertEqual(outcome.reason, "QUEUE_FULL")
        XCTAssertEqual(outcome.provider, "OpenAI")
        XCTAssertFalse(outcome.failed)
        XCTAssertNil(FallbackOutcome(nil))
    }

    func testProtocolLabelsAreStable() {
        XCTAssertEqual(FallbackProtocol.all.count, 3)
        XCTAssertEqual(FallbackProtocol.labelKey("openai-chat"), "OpenAI Chat Completions API")
        XCTAssertEqual(FallbackProtocol.labelKey("openai-responses"), "OpenAI Responses API")
        XCTAssertEqual(FallbackProtocol.labelKey("anthropic-messages"), "Anthropic Messages API")
    }
}
