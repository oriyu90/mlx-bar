import AppKit
import UniformTypeIdentifiers

@MainActor
final class FileSelectionService {
    static let shared = FileSelectionService()

    enum LibraryLocation {
        case user
        case system

        var url: URL {
            switch self {
            case .user:
                FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library", isDirectory: true)
            case .system:
                URL(fileURLWithPath: "/Library", isDirectory: true)
            }
        }
    }

    /// Outcome of a picker request, so a caller can tell "nothing chosen" from
    /// "another picker is already open" instead of silently doing nothing.
    enum Selection {
        case chosen([URL])
        case cancelled
        case busy
    }

    private var activePanel: NSOpenPanel?

    func chooseFolder(startingAt directory: URL? = nil) async -> Selection {
        let panel = NSOpenPanel()
        panel.title = LS("モデルフォルダを選択")
        panel.message = LS("隠しフォルダも表示しています。選択したフォルダは読み取り専用で走査されます。")
        panel.prompt = LS("追加")
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true
        panel.treatsFilePackagesAsDirectories = true
        panel.directoryURL = validDirectory(directory) ?? FileManager.default.homeDirectoryForCurrentUser
        return await present(panel)
    }

    func chooseImages(startingAt directory: URL? = nil) async -> Selection {
        let panel = NSOpenPanel()
        panel.title = LS("画像を選択")
        panel.prompt = LS("追加")
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = [.image]
        panel.showsHiddenFiles = true
        panel.directoryURL = validDirectory(directory)
        return await present(panel)
    }

    private func validDirectory(_ url: URL?) -> URL? {
        guard let url else { return nil }
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory) && isDirectory.boolValue
            ? url : nil
    }

    private func present(_ panel: NSOpenPanel) async -> Selection {
        if let activePanel {
            NSApplication.shared.activate(ignoringOtherApps: true)
            activePanel.makeKeyAndOrderFront(nil)
            return .busy
        }

        activePanel = panel
        panel.level = .floating
        panel.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        NSApplication.shared.activate(ignoringOtherApps: true)

        return await withCheckedContinuation { continuation in
            panel.begin { [weak self] response in
                let urls = response == .OK ? panel.urls : []
                panel.orderOut(nil)
                self?.activePanel = nil
                continuation.resume(returning: urls.isEmpty ? .cancelled : .chosen(urls))
            }
            panel.makeKeyAndOrderFront(nil)
        }
    }
}
