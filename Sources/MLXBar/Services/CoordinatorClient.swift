import Foundation
import ServiceManagement

enum ClientError: LocalizedError {
    case unavailable
    case timedOut
    case command(String)

    var errorDescription: String? {
        switch self {
        case .unavailable: "MLXBarサービスを起動できません"
        case .timedOut: "MLXBarサービスが応答しません。しばらくしてからもう一度お試しください。"
        case .command(let message): message
        }
    }
}

final class CoordinatorClient: @unchecked Sendable {
    struct StreamEvent: Sendable {
        let type: String
        let text: String?
        let message: String?
        let generationTPS: Double?
    }
    private let serviceLabel = "com.yukiorita.MLXBar.Coordinator"
    private var socketPath: String {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MLXBar/control/coordinator.sock").path
    }

    /// How long a management call may run before curl gives up.
    ///
    /// Without a ceiling, a wedged coordinator leaves the caller awaiting
    /// forever, and the view model's `busy` flag never clears — every button in
    /// the app stays disabled behind a spinner with no way back short of a
    /// relaunch. Model loading and runtime work legitimately take minutes, so
    /// those callers raise the limit rather than sharing the default.
    enum Timeout {
        static let standard = 30
        static let modelLoad = 900
        static let runtime = 3600
    }

    func request(_ method: String, _ path: String, bodyJSON: String? = nil,
                 timeoutSeconds: Int = Timeout.standard) async throws -> Data {
        var arguments = ["--silent", "--show-error", "--fail-with-body", "--unix-socket", socketPath,
                         "--connect-timeout", "5", "--max-time", String(timeoutSeconds),
                         "--request", method, "--header", "Content-Type: application/json"]
        if let bodyJSON {
            arguments += ["--data-binary", bodyJSON]
        }
        arguments.append("http://mlxbar\(path)")
        do {
            return try await runProcess(URL(fileURLWithPath: "/usr/bin/curl"), arguments,
                                        timeoutStatuses: [28])
        } catch ClientError.command(let rawMessage) {
            // curl writes the HTTP body to stdout and its diagnostic to stderr.
            // runProcess joins both, so parse the body as a whole and keep the
            // structured API error instead of exposing JSON and curl internals.
            if let detail = Self.apiErrorDetail(in: rawMessage) {
                throw ClientError.command(detail)
            }
            throw ClientError.command(rawMessage)
        }
    }

    /// Extracts the coordinator's error message from a mixed stdout/stderr blob.
    ///
    /// The body may be pretty-printed across several lines, so the whole text is
    /// tried first and individual lines only as a fallback.
    static func apiErrorDetail(in raw: String) -> String? {
        for candidate in [raw] + raw.split(separator: "\n").map(String.init) {
            guard let data = candidate.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            let detail = (object["detail"] as? [String: Any]) ?? (object["error"] as? [String: Any])
            guard let detail else { continue }
            if let message = detail["message"] as? String, !message.isEmpty { return message }
            if let code = detail["code"] as? String, !code.isEmpty { return code }
        }
        return nil
    }

    /// Streams server-sent events from the coordinator.
    ///
    /// stdout and stderr are both drained from readability handlers rather than
    /// polled with `availableData`: the old loop blocked a cooperative thread
    /// for the whole generation, and leaving stderr unread until exit could
    /// deadlock curl once the OS pipe buffer filled.
    func stream(_ path: String, bodyJSON: String, onEvent: @escaping @Sendable (StreamEvent) -> Void) async throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        process.arguments = ["--silent", "--show-error", "--fail-with-body", "--no-buffer",
                             "--connect-timeout", "5", "--max-time", "7210",
                             "--unix-socket", socketPath, "--request", "POST",
                             "--header", "Content-Type: application/json", "--data-binary", bodyJSON,
                             "http://mlxbar\(path)"]
        let output = Pipe(), error = Pipe()
        process.standardOutput = output; process.standardError = error

        let buffer = StreamBuffer(onEvent: onEvent)
        output.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty { return }
            buffer.append(chunk)
        }
        error.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            if chunk.isEmpty { return }
            buffer.appendDiagnostic(chunk)
        }
        // If `process.run()` throws below, the continuation is resumed before
        // this function reaches its normal cleanup, which would otherwise
        // leave these handlers registered forever — pinning their pipes and,
        // transitively, `buffer`/`onEvent` (and whatever `self` the caller's
        // closure captured) alive with no way to release them.
        defer {
            output.fileHandleForReading.readabilityHandler = nil
            error.fileHandleForReading.readabilityHandler = nil
        }

        let status: Int32 = try await withCheckedThrowingContinuation { continuation in
            process.terminationHandler = { process in
                continuation.resume(returning: process.terminationStatus)
            }
            do { try process.run() } catch { continuation.resume(throwing: error) }
        }
        // Drain whatever the handlers had not yet been scheduled for.
        buffer.append(output.fileHandleForReading.readDataToEndOfFile())
        buffer.appendDiagnostic(error.fileHandleForReading.readDataToEndOfFile())

        guard status == 0 else {
            if status == 28 { throw ClientError.timedOut }
            let raw = buffer.diagnosticText
            throw ClientError.command(Self.apiErrorDetail(in: raw)
                                      ?? (raw.isEmpty ? "生成に失敗しました" : raw))
        }
    }

    /// Reassembles SSE lines arriving in arbitrary chunks off the reader queue.
    private final class StreamBuffer: @unchecked Sendable {
        private let lock = NSLock()
        private var pending = Data()
        private var diagnostic = Data()
        private let onEvent: @Sendable (StreamEvent) -> Void

        init(onEvent: @escaping @Sendable (StreamEvent) -> Void) { self.onEvent = onEvent }

        var diagnosticText: String {
            lock.lock(); defer { lock.unlock() }
            return String(data: diagnostic, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        }

        func appendDiagnostic(_ chunk: Data) {
            guard !chunk.isEmpty else { return }
            lock.lock(); defer { lock.unlock() }
            diagnostic.append(chunk)
        }

        func append(_ chunk: Data) {
            guard !chunk.isEmpty else { return }
            var events: [StreamEvent] = []
            lock.lock()
            pending.append(chunk)
            while let newline = pending.firstIndex(of: 0x0A) {
                let lineData = pending[..<newline]
                pending.removeSubrange(...newline)
                guard var line = String(data: lineData, encoding: .utf8) else { continue }
                line = line.trimmingCharacters(in: .whitespacesAndNewlines)
                guard line.hasPrefix("data: "),
                      let data = String(line.dropFirst(6)).data(using: .utf8),
                      let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                events.append(StreamEvent(type: event["type"] as? String ?? "unknown",
                                          text: event["text"] as? String,
                                          message: event["message"] as? String,
                                          generationTPS: (event["generation_tps"] as? NSNumber)?.doubleValue))
            }
            lock.unlock()
            for event in events { onEvent(event) }
        }
    }

    func startService() async throws {
        if await isExpectedVersionHealthy() { return }
        let service = SMAppService.agent(plistName: "com.yukiorita.MLXBar.Coordinator.plist")
        var registrationError: Error?
        switch service.status {
        case .enabled:
            _ = try? await launchctl(["kickstart", "-k", "gui/\(getuid())/\(serviceLabel)"])
        case .requiresApproval:
            throw ClientError.command("バックグラウンド実行の許可が必要です。システム設定のログイン項目でMLXBarを許可してください。")
        case .notRegistered, .notFound:
            do { try service.register() } catch { registrationError = error }
        @unknown default:
            break
        }

        if await waitUntilExpectedVersionHealthy(seconds: 5) { return }
        do {
            try await installFallbackLaunchAgent()
        } catch {
            let primary = registrationError.map { "ServiceManagement: \($0.localizedDescription)。" } ?? ""
            throw ClientError.command("\(primary)バックエンドサービスを登録できません: \(error.localizedDescription)")
        }
        if await waitUntilExpectedVersionHealthy(seconds: 10) { return }
        throw ClientError.unavailable
    }

    func removeAllData() async throws {
        // Stop active work first so partially-created runtime environments are
        // cleaned by the coordinator before its launch agent is unloaded.
        if let data = try? await request("GET", "/api/v1/runtimes"),
           let runtimes = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for engine in ["mlx-lm", "mlx-vlm"] {
                guard let runtime = runtimes[engine] as? [String: Any],
                      let job = runtime["activeJob"] as? [String: Any],
                      let jobID = job["id"] as? String else { continue }
                _ = try? await request("POST", "/api/v1/runtimes/\(engine)/jobs/\(jobID)/cancel")
            }
        }
        _ = try? await request("DELETE", "/api/v1/models/loaded")

        let domain = "gui/\(getuid())"
        let fallbackPlist = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(serviceLabel).plist")
        _ = try? await launchctl(["bootout", "\(domain)/\(serviceLabel)"], timeoutSeconds: 10)
        _ = try? await launchctl(["bootout", domain, fallbackPlist.path], timeoutSeconds: 10)

        let service = SMAppService.agent(plistName: "com.yukiorita.MLXBar.Coordinator.plist")
        try? await service.unregister()
        try? await SMAppService.mainApp.unregister()

        for _ in 0..<20 {
            if !(await isHealthy()) { break }
            try? await Task.sleep(for: .milliseconds(100))
        }
        if await isHealthy() {
            throw ClientError.command("バックグラウンドサービスを停止できなかったため、データ削除を中止しました")
        }

        _ = try? await runProcess(URL(fileURLWithPath: "/usr/bin/defaults"),
                                  ["delete", "com.yukiorita.MLXBar"], timeoutSeconds: 10)

        let home = FileManager.default.homeDirectoryForCurrentUser
        let paths = [
            home.appendingPathComponent("Library/Application Support/MLXBar"),
            home.appendingPathComponent("Library/Logs/MLXBar"),
            home.appendingPathComponent("Library/Caches/com.yukiorita.MLXBar"),
            home.appendingPathComponent("Library/Saved Application State/com.yukiorita.MLXBar.savedState"),
            home.appendingPathComponent("Library/Preferences/com.yukiorita.MLXBar.plist"),
            fallbackPlist,
            URL(fileURLWithPath: "/tmp/mlxbar-coordinator.log"),
            URL(fileURLWithPath: "/tmp/mlxbar-dev.log"),
        ]
        var removalErrors: [String] = []
        for path in paths where FileManager.default.fileExists(atPath: path.path) {
            do { try FileManager.default.removeItem(at: path) }
            catch { removalErrors.append("\(path.lastPathComponent): \(error.localizedDescription)") }
        }
        if !removalErrors.isEmpty {
            throw ClientError.command(removalErrors.joined(separator: "、"))
        }
    }

    private func isHealthy() async -> Bool {
        (try? await request("GET", "/api/v1/health")) != nil
    }

    private func isExpectedVersionHealthy() async -> Bool {
        guard let data = try? await request("GET", "/api/v1/health"),
              let health = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        guard let expected = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
              !expected.isEmpty else { return true }
        return health["status"] as? String == "ok" && health["version"] as? String == expected
    }

    private func waitUntilExpectedVersionHealthy(seconds: Int) async -> Bool {
        for _ in 0..<(seconds * 5) {
            if await isExpectedVersionHealthy() { return true }
            try? await Task.sleep(for: .milliseconds(200))
        }
        return false
    }

    private func installFallbackLaunchAgent() async throws {
        guard let coordinator = Bundle.main.executableURL?.deletingLastPathComponent()
            .appendingPathComponent("MLXBarCoordinator"),
              FileManager.default.isExecutableFile(atPath: coordinator.path) else {
            throw ClientError.command("Coordinator実行ファイルが見つかりません")
        }
        let launchAgents = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
        try FileManager.default.createDirectory(at: launchAgents, withIntermediateDirectories: true)
        let plistURL = launchAgents.appendingPathComponent("\(serviceLabel).plist")
        let bundleResources = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/MLXBar_MLXBar.bundle")
        // No StandardOutPath/StandardErrorPath: the coordinator writes its own
        // log inside Application Support, which stays private to this user.
        let plist: [String: Any] = [
            "Label": serviceLabel, "ProgramArguments": [coordinator.path], "RunAtLoad": true,
            "KeepAlive": ["SuccessfulExit": false], "ProcessType": "Interactive", "ThrottleInterval": 10,
            "EnvironmentVariables": ["PATH": "\(bundleResources.path):/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"]
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        let temporary = plistURL.appendingPathExtension("tmp")
        try data.write(to: temporary, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
        _ = try? FileManager.default.removeItem(at: plistURL)
        try FileManager.default.moveItem(at: temporary, to: plistURL)
        let domain = "gui/\(getuid())"
        _ = try? await launchctl(["bootout", domain, plistURL.path])
        try await launchctl(["bootstrap", domain, plistURL.path])
        try await launchctl(["kickstart", "-k", "\(domain)/\(serviceLabel)"])
    }

    @discardableResult
    private func launchctl(_ arguments: [String], timeoutSeconds: Int? = nil) async throws -> Data {
        try await runProcess(URL(fileURLWithPath: "/bin/launchctl"), arguments, timeoutSeconds: timeoutSeconds)
    }

    /// Guards a `CheckedContinuation` so two racing completion paths — a
    /// process's `terminationHandler` and a timeout watchdog — can each try to
    /// resume it without the second one tripping `withCheckedThrowingContinuation`'s
    /// fatal "resumed more than once" check.
    private final class SingleResume: @unchecked Sendable {
        private let lock = NSLock()
        private var didResume = false
        func run(_ body: () -> Void) {
            lock.lock()
            let alreadyResumed = didResume
            didResume = true
            lock.unlock()
            guard !alreadyResumed else { return }
            body()
        }
    }

    /// Runs a process and resolves once, either when it exits or — if
    /// `timeoutSeconds` is given — when the watchdog fires first.
    ///
    /// Unlike `request()`'s curl calls (which have `--max-time` built in),
    /// plain `launchctl`/`defaults` invocations have no self-timeout: a wedged
    /// `launchctl bootout` used to leave the awaiting call (and callers like
    /// `removeAllData()`'s `busy`/`isRemovingAllData` spinner) hanging with no
    /// way back short of a relaunch.
    private func runProcess(_ executable: URL, _ arguments: [String],
                            timeoutStatuses: Set<Int32> = [],
                            timeoutSeconds: Int? = nil) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = executable; process.arguments = arguments
            let output = Pipe(), error = Pipe()
            process.standardOutput = output; process.standardError = error

            let resume = SingleResume()
            process.terminationHandler = { process in
                let data = output.fileHandleForReading.readDataToEndOfFile()
                resume.run {
                    if process.terminationStatus == 0 {
                        continuation.resume(returning: data)
                    } else if timeoutStatuses.contains(process.terminationStatus) {
                        continuation.resume(throwing: ClientError.timedOut)
                    } else {
                        let stderr = String(data: error.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                        let stdout = String(data: data, encoding: .utf8) ?? ""
                        continuation.resume(throwing: ClientError.command([stdout, stderr].filter { !$0.isEmpty }.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)))
                    }
                }
            }
            if let timeoutSeconds {
                Task {
                    try? await Task.sleep(for: .seconds(timeoutSeconds))
                    guard process.isRunning else { return }
                    process.terminate()
                    resume.run { continuation.resume(throwing: ClientError.timedOut) }
                }
            }
            do { try process.run() } catch { resume.run { continuation.resume(throwing: error) } }
        }
    }
}
