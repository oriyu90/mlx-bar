import SwiftUI

/// First-launch setup wizard (v2.6.0).
///
/// Shown only when the coordinator reports `required` (fresh installs; never
/// on updates). Three questions — model folder, simultaneous-model count,
/// bulk preset — then a summary. Skipping (or closing the window) records
/// completion without changing anything. Plain VStack/ScrollView: no
/// NavigationSplitView, so none of the Settings layout traps apply.
struct SetupWizardView: View {
    @ObservedObject var model: MenuBarViewModel
    @State private var step = 0
    @State private var folder: String?
    @State private var maxResident = 2
    @State private var preset = "light"
    @State private var isSelectingFolder = false

    private var hardware: [String: Any] {
        model.setupStatus["hardware"] as? [String: Any] ?? [:]
    }

    private var defaultFolder: String {
        (hardware["lmStudio"] as? [String: Any])?["defaultFolder"] as? String
        ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".lmstudio/models", isDirectory: true).path
    }

    private var showingFolder: String { folder ?? defaultFolder }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(LS("MLXBarの初期設定")).font(.title2.bold())
            Text(LS("お使いのPCに合わせた設定を行います。すべての項目に推奨値が入っているため、そのまま完了できます。"))
                .font(.callout).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if step == 0 { folderStep }
                    else if step == 1 { countStep }
                    else if step == 2 { presetStep }
                    else { summaryStep }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let message = model.setupMessage {
                Text(message).font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            HStack {
                Button(LS("スキップ")) {
                    Task { await model.completeSetup(skipped: true) }
                }
                .buttonStyle(.link)
                Spacer()
                if step > 0 {
                    Button(LS("戻る")) { step -= 1 }
                }
                if step < 3 {
                    Button(LS("次へ")) { step += 1 }.buttonStyle(.borderedProminent)
                } else {
                    Button(LS("完了")) {
                        Task { await model.completeSetup(skipped: false, choices: choices()) }
                    }.buttonStyle(.borderedProminent)
                }
            }
        }
        .padding(20)
        .task { seedDefaults() }
        .onDisappear {
            // Closing the window counts as a skip so the wizard never nags
            // again; explicit completion already flipped the flag.
            guard model.showSetupWizard else { return }
            Task { await model.completeSetup(skipped: true) }
        }
    }

    private var folderStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(LS("1. モデルのフォルダ")).font(.headline)
            Text(LS("モデルを探す追加フォルダを選びます。基本はLM Studioのモデルフォルダ（下の推奨値）で、そのままで構いません。"))
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(showingFolder).font(.callout.monospaced())
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(LS("フォルダを選択…")) { chooseFolder() }.disabled(isSelectingFolder)
        }
    }

    private var countStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(LS("2. 同時に使うモデル数")).font(.headline)
            Text(LS("同時に常駐させるモデルの上限です。メモリに合わせた推奨値が入っています。"))
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            Stepper(value: $maxResident, in: 1...8) {
                Text("\(maxResident)")
            }
        }
    }

    private var presetStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(LS("3. 一括設定モード")).font(.headline)
            Picker(LS("モード"), selection: $preset) {
                Text(LS("軽量")).tag("light")
                Text(LS("フル")).tag("full")
            }.pickerStyle(.radioGroup)
            Text(LS("軽量はKVキャッシュ8bit・メモリ上限はPCの80%で、すぐに使えます。フルはKVキャッシュbf16・ディスク少し多めです。あとから設定画面で変更できます。"))
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var summaryStep: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(LS("確認")).font(.headline)
            LabeledContent(LS("モデルフォルダ"), value: showingFolder)
            LabeledContent(LS("同時に使うモデル数"), value: "\(maxResident)")
            LabeledContent(LS("一括設定モード"),
                           value: preset == "light" ? LS("軽量") : LS("フル"))
            Text(LS("完了するとモデルを再スキャンします。スキップした場合はすべて既定値のままです。"))
                .font(.caption).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func choices() -> [String: Any] {
        var result: [String: Any] = [
            "maxResidentModels": maxResident,
            "preset": preset,
        ]
        if let folder { result["modelsFolder"] = folder }
        return result
    }

    private func seedDefaults() {
        if let count = hardware["defaultMaxResidentModels"] as? Int {
            maxResident = min(8, max(1, count))
        }
    }

    private func chooseFolder() {
        isSelectingFolder = true
        Task { @MainActor in
            defer { isSelectingFolder = false }
            switch await FileSelectionService.shared.chooseFolder() {
            case .chosen(let urls):
                if let url = urls.first { folder = url.path }
            case .busy:
                model.setupMessage = LS("別のファイル選択画面が開いています。先にそちらを閉じてください。")
            case .cancelled:
                break
            }
        }
    }
}
